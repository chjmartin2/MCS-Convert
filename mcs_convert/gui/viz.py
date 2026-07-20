"""Playback visualization windows: VU meters, spectrum analyzer, and the
DOS-replica preview.

All windows share one data source: the app's rendered buffers (master, per-voice
arrays, sample rate) plus the elapsed playback position. Each exposes
`draw(elapsed)` — called from the app's playhead loop ~30x/sec — and accepts
None to flatline. The DOS replica window imitates the standalone `.COM`
players' on-screen visualizations (the text-mode combined monitor, spectrum
analyzer, VU meters...) so the export dialog can preview what a chosen scope
will look like before building the .COM.
"""

from __future__ import annotations

import tkinter as tk
from typing import List, Optional

import numpy as np

_BG = "#14161c"
_ACCENT = "#7fd17f"

# DOS text-mode palette (the CGA/VGA colors the .COM scopes use)
_DOS = {"yellow": "#ffff55", "red": "#aa0000", "brightred": "#ff5555",
        "blue": "#0000aa", "green": "#55ff55", "white": "#ffffff",
        "grey": "#aaaaaa", "cyan": "#55ffff", "black": "#000000"}
#: per-channel DOS colors (matches the .COM _TATTR: yellow/red/blue/green + white)
_DOS_CH = ("#ffff55", "#ff5555", "#5555ff", "#55ff55", "#ffffff")


def _window(parent, title: str, minsize=(360, 240)):
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=_BG)
    win.minsize(*minsize)
    return win


def _rms_levels(voices, idx: int, span: int) -> List[float]:
    """Per-voice RMS of the ~30 ms window at idx, scaled to 0..1."""
    out = []
    for v in voices:
        seg = v[idx:idx + span] if v is not None and idx < len(v) else None
        if seg is None or len(seg) == 0:
            out.append(0.0)
        else:
            out.append(min(1.0, float(np.sqrt(np.mean(seg ** 2))) * 2.2))
    return out


def _spectrum(master, idx: int, sr: int, bars: int = 20) -> List[float]:
    """Log-spaced spectrum magnitudes 0..1 for the window at idx."""
    n = 2048
    seg = master[idx:idx + n] if master is not None and idx < len(master) else None
    if seg is None or len(seg) < 256:
        return [0.0] * bars
    if len(seg) < n:
        seg = np.pad(seg, (0, n - len(seg)))
    mag = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    freqs = np.fft.rfftfreq(len(seg), 1.0 / sr)
    edges = np.geomspace(55.0, min(8000.0, sr / 2 - 1), bars + 1)
    out = []
    for i in range(bars):
        band = mag[(freqs >= edges[i]) & (freqs < edges[i + 1])]
        out.append(min(1.0, float(band.max()) / 60.0) if len(band) else 0.0)
    return out


class VUWindow:
    """Per-track VU meters: horizontal bars with green/yellow/red zones and a
    falling white peak tick — the standalone player's meters, native-styled."""

    def __init__(self, parent, names: List[str]) -> None:
        self.win = _window(parent, "MCS-Convert — VU Meters",
                           (420, 40 * max(1, len(names)) + 40))
        self.canvas = tk.Canvas(self.win, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.names = names
        self.levels = [0.0] * len(names)
        self.peaks = [0.0] * len(names)

    def alive(self) -> bool:
        return bool(self.win.winfo_exists())

    def draw(self, levels: Optional[List[float]]) -> None:
        if not self.alive():
            return
        c = self.canvas
        w = max(c.winfo_width(), 100)
        h = max(c.winfo_height(), 60)
        n = max(1, len(self.names))
        row_h = h / n
        c.delete("all")
        for i, name in enumerate(self.names):
            lv = 0.0 if levels is None else levels[i] if i < len(levels) else 0.0
            # attack fast, release slow; peak falls slower still
            self.levels[i] = max(lv, self.levels[i] * 0.82)
            self.peaks[i] = max(self.levels[i], self.peaks[i] - 0.012)
            y0 = i * row_h + row_h * 0.25
            y1 = (i + 1) * row_h - row_h * 0.25
            x0, x1 = 90, w - 16
            c.create_text(8, (y0 + y1) / 2, text=name, anchor="w",
                          fill=_DOS_CH[i % len(_DOS_CH)],
                          font=("Consolas", 10, "bold"))
            c.create_rectangle(x0, y0, x1, y1, outline="#333333")
            span = x1 - x0
            fill_to = x0 + span * self.levels[i]
            for frac, color in ((0.65, "#22cc22"), (0.85, "#cccc22"), (1.0, "#cc2222")):
                seg_end = min(fill_to, x0 + span * frac)
                seg_start = x0 + span * (0 if frac == 0.65 else (0.65 if frac == 0.85 else 0.85))
                if seg_end > seg_start:
                    c.create_rectangle(seg_start, y0 + 1, seg_end, y1 - 1,
                                       fill=color, outline="")
            px = x0 + span * self.peaks[i]
            c.create_line(px, y0, px, y1, fill="#ffffff", width=2)


class SpectrumWindow:
    """A live spectrum analyzer: log-spaced bars colored by height (green ->
    yellow -> red) with white falling peak caps — the .COM text-4 display's
    big sibling."""

    def __init__(self, parent, bars: int = 20) -> None:
        self.win = _window(parent, "MCS-Convert — Spectrum Analyzer")
        self.canvas = tk.Canvas(self.win, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.nbars = bars
        self.levels = [0.0] * bars
        self.peaks = [0.0] * bars

    def alive(self) -> bool:
        return bool(self.win.winfo_exists())

    def draw(self, spectrum: Optional[List[float]]) -> None:
        if not self.alive():
            return
        c = self.canvas
        w = max(c.winfo_width(), 100)
        h = max(c.winfo_height(), 60)
        c.delete("all")
        bw = w / self.nbars
        for i in range(self.nbars):
            lv = 0.0 if spectrum is None else spectrum[i]
            self.levels[i] = max(lv, self.levels[i] - 0.06)      # slow release
            self.peaks[i] = max(self.levels[i], self.peaks[i] - 0.008)
            x0 = i * bw + 2
            x1 = (i + 1) * bw - 2
            top = h - 8 - (h - 24) * self.levels[i]
            # colored by height: green base, yellow middle, red top
            for frac, color in ((0.5, "#22cc22"), (0.8, "#cccc22"), (1.0, "#cc2222")):
                zone_top = h - 8 - (h - 24) * min(self.levels[i], frac)
                zone_bot = h - 8 - (h - 24) * (0 if frac == 0.5 else (0.5 if frac == 0.8 else 0.8))
                if zone_top < zone_bot:
                    c.create_rectangle(x0, zone_top, x1, zone_bot, fill=color, outline="")
            py = h - 8 - (h - 24) * self.peaks[i]
            c.create_rectangle(x0, py - 2, x1, py, fill="#ffffff", outline="")


#: DOS-replica styles the preview window can imitate (.COM scope names).
#: Every distinct replica the DOS preview can show — one per .COM visualization
#: (plus the MCS notation view for the .MCS target).
DOS_STYLES = ("combined monitor (text 5)", "spectrum analyzer (text 4)",
              "VU meters", "line scopes (text 3)", "line trace (text 2)",
              "block scopes (text 1)", "Tandy graphics", "VGA 256",
              "static poster", "MCS notation")

#: CGA mode-4 palette-0 colours (the static poster) and mode-9/VGA channel sets.
_CGA4 = {"black": "#000000", "green": "#55ff55", "red": "#ff5555",
         "yellow": "#ffff55"}
_TANDY_CH = ("#ffff55", "#aa0000", "#5555ff", "#55ff55")   # mode-9 packed colours
_VGA_CH = ("#ffff55", "#ff5555", "#55ffff", "#55ff55")     # mode-13h indices


class DosVizWindow:
    """A replica of the standalone .COM players' DOS visualizations, drawn on a
    320x200-proportioned canvas with the DOS palette — so "what will this scope
    look like?" is answered before the .COM is ever built. The style mirrors
    the export dialog's Visualization selection (and the .MCS target swaps in
    the scrolling music-notation view, drawn from the ENCODED staff records)."""

    def __init__(self, parent, style: str = DOS_STYLES[0],
                 names: Optional[List[str]] = None) -> None:
        self.win = _window(parent, f"DOS preview — {style}", (480, 320))
        self.canvas = tk.Canvas(self.win, bg="#000000", highlightthickness=0,
                                width=640, height=400)
        self.canvas.pack(fill="both", expand=True)
        self.style = style
        self.names = (names or ["P1", "P2", "Tr", "Nz"])[:4]
        self.vu = [0.0] * 4
        self.vupk = [0.0] * 4
        self.spec = [0.0] * 18
        self.phase = 0.0
        self.song = None                 # for the static poster (set_song)
        self.staves = None               # for MCS notation (set_notation)
        self.bar_ticks = 32
        self.step_seconds = 0.075
        self.elapsed = 0.0

    def set_style(self, style: str) -> None:
        """Switch the replica live (the window retitles to match)."""
        if style != self.style:
            self.style = style
            if self.alive():
                self.win.title(f"DOS preview — {style}")

    def set_song(self, song, step_seconds: float) -> None:
        """Give the poster painter the whole song (it draws time x pitch)."""
        self.song = song
        self.step_seconds = step_seconds

    def set_notation(self, staves, bar_ticks: int, step_seconds: float) -> None:
        """Give the notation painter the DECODED MCS staff records — the exact
        vertical positions / x-slots / symbols the 1984 program draws from."""
        self.staves = staves
        self.bar_ticks = max(1, bar_ticks)
        self.step_seconds = step_seconds

    def alive(self) -> bool:
        return bool(self.win.winfo_exists())

    # -- shared painters ----------------------------------------------------
    def _grid_cell(self):
        c = self.canvas
        w = max(c.winfo_width(), 320)
        h = max(c.winfo_height(), 200)
        return c, w, h

    def _scope_trace(self, c, x0, y0, x1, y1, level: float, period: float,
                     color: str, line: bool) -> None:
        """One channel's square-wave trace: sounding -> a scrolling square of
        that period; silent -> the center line."""
        mid = (y0 + y1) / 2
        amp = (y1 - y0) * 0.32
        if level <= 0.001:
            c.create_line(x0, mid, x1, mid, fill=color)
            return
        pts = []
        px = max(6.0, period * 26.0)                 # on-screen half-period
        x = x0
        state = int((self.phase * 60) / px) & 1
        while x < x1:
            nxt = min(x1, x + px)
            y = mid - amp if state else mid + amp
            if line and pts:
                pts.extend([x, y])
            pts.extend([x, y, nxt, y])
            x = nxt
            state ^= 1
        c.create_line(*pts, fill=color, width=2)

    # -- draw ---------------------------------------------------------------
    def draw(self, levels, spectrum, periods, elapsed: float = 0.0) -> None:
        """levels: per-voice 0..1 (None = flat); spectrum: 18+ mags; periods:
        per-voice relative wave periods 0..1 (pitch -> tightness); elapsed:
        playback seconds (drives the poster playhead + notation scroll)."""
        if not self.alive():
            return
        c, w, h = self._grid_cell()
        c.delete("all")
        c.configure(bg="#ffffff" if self.style.startswith("MCS") else "#000000")
        self.phase += 1.0
        self.elapsed = elapsed
        lv = levels or [0.0] * 4
        sp = spectrum or [0.0] * 18
        pr = periods or [0.3] * 4
        style = self.style
        if style.startswith("combined"):
            self._draw_combined(c, w, h, lv, sp, pr)
        elif style.startswith("spectrum"):
            self._draw_spectrum(c, w, h, sp, full=True)
        elif style.startswith("VU"):
            self._draw_vu(c, w, h, lv, 0, 0, w, h, big=True)
        elif style.startswith("line trace"):           # text 2: stacked traces
            self._draw_bands(c, w, h, lv, pr)
        elif style.startswith("Tandy") or style.startswith("VGA"):
            # the graphics scopes: 2x2 grid + the master pane below, in the
            # mode's own palette (mode-9 packed colours vs mode-13h indices)
            colors = _TANDY_CH if style.startswith("Tandy") else _VGA_CH
            self._draw_scopegrid(c, w, h, lv, pr, 0, 0, w, h * 0.62, line=True,
                                 colors=colors)
            self._draw_master(c, 0, h * 0.62, w, h, lv, colors)
        elif style.startswith("static"):
            self._draw_poster(c, w, h)
        elif style.startswith("MCS"):
            self._draw_notation(c, w, h)
        else:
            line = style.startswith("line")
            self._draw_scopegrid(c, w, h, lv, pr, 0, 0, w, h, line=line)

    def _draw_combined(self, c, w, h, lv, sp, pr) -> None:
        """The text-5 combined monitor: 2x2 scopes on top, spectrum bottom-left,
        VU meters bottom-right, one grey grid."""
        self._draw_scopegrid(c, w, h, lv, pr, 0, 0, w, h * 0.5, line=True)
        self._draw_spectrum(c, w, h, sp, region=(0, h * 0.5, w * 0.5, h))
        self._draw_vu(c, w, h, lv, w * 0.5, h * 0.5, w, h)
        grey = _DOS["grey"]
        c.create_rectangle(2, 2, w - 2, h - 2, outline=grey)
        c.create_line(2, h * 0.5, w - 2, h * 0.5, fill=grey)
        c.create_line(w * 0.5, 2, w * 0.5, h - 2, fill=grey)
        c.create_line(2, h * 0.25, w - 2, h * 0.25, fill=grey)

    def _draw_master(self, c, x0, y0, x1, y1, lv, colors) -> None:
        """The graphics scopes' framed master pane: the summed square trace."""
        grey = _DOS["grey"]
        c.create_rectangle(x0 + 4, y0 + 4, x1 - 4, y1 - 4, outline=_DOS["white"])
        total = min(1.0, sum(lv[:3]) / 3.0)
        self._scope_trace(c, x0 + 10, y0 + 8, x1 - 10, y1 - 8, total, 0.35,
                          _DOS["white"], True)
        c.create_text(x0 + 12, y0 + 8, text="master", anchor="nw", fill=grey,
                      font=("Consolas", 9))

    def _draw_bands(self, c, w, h, lv, pr) -> None:
        """text 2: full-width box-drawing line traces, one band per voice plus
        the master, stacked — each a single connected square trace."""
        n = 5
        band = h / n
        for k in range(n):
            y0 = k * band
            if k < 4:
                color = _DOS_CH[k]
                name = self.names[k]
                level, period = lv[k], pr[k]
            else:
                color = _DOS["white"]
                name = "master"
                level, period = min(1.0, sum(lv[:3]) / 3.0), 0.35
            c.create_text(8, y0 + 6, text=name, anchor="nw", fill=color,
                          font=("Consolas", 9))
            self._scope_trace(c, 60, y0 + 4, w - 8, y0 + band - 4,
                              level, period, color, True)
            c.create_line(2, y0 + band, w - 2, y0 + band, fill=_DOS["grey"])

    def _draw_poster(self, c, w, h) -> None:
        """The static-screen CGA poster: the WHOLE song as a piano roll in the
        black/green/red/yellow palette, drawn once per frame from the song (a
        faithful preview of dosplayer's baked 320x200x4 image), with a thin
        playhead the real DOS build doesn't have."""
        song = self.song
        if song is None:
            c.create_text(w / 2, h / 2, text="(no song)", fill=_CGA4["green"])
            return
        tone_tracks = [t for t in song.tracks
                       if getattr(t, "kind", "tone") == "tone"][:3]
        notes = [(n.start_tick, n.end_tick, n.midi_note, vi)
                 for vi, t in enumerate(tone_tracks)
                 for n in t.notes if not n.is_rest and not n.percussive]
        perc = [n.start_tick for t in song.tracks for n in t.notes
                if not n.is_rest and (n.percussive
                                      or getattr(t, "kind", "tone") in
                                      ("noise", "drum"))]
        if not notes:
            return
        tot = max(e for _, e, _, _ in notes) or 1
        lo = min(m for _, _, m, _ in notes)
        hi = max(m for _, _, m, _ in notes)
        span = max(1, hi - lo)
        top, bot = 14, h - 24
        colors = (_CGA4["yellow"], _CGA4["green"], _CGA4["red"])   # lead/2nd/bass
        for cc in range(12, 128, 12):                 # dotted octave gridlines
            if lo <= cc <= hi:
                y = bot - (cc - lo) * (bot - top) / span
                for x in range(2, int(w) - 2, 12):
                    c.create_line(x, y, x + 4, y, fill="#1a5c1a")
        for vi in (2, 1, 0):                          # bass under, lead on top
            for s, e, m, v in notes:
                if v != vi:
                    continue
                y = bot - (m - lo) * (bot - top) / span
                c.create_rectangle(2 + s * (w - 4) / tot, y - 1,
                                   2 + e * (w - 4) / tot, y + 1,
                                   fill=colors[vi], outline="")
        for s in perc:                                # drum strip along the bottom
            x = 2 + s * (w - 4) / tot
            c.create_line(x, h - 18, x, h - 8, fill=_CGA4["yellow"])
        for i, (label, colr) in enumerate((("P1", "yellow"), ("P2", "green"),
                                           ("Tri", "red"), ("Drums", "yellow"))):
            c.create_text(8 + i * 46, 4, text=label, anchor="nw",
                          fill=_CGA4[colr], font=("Consolas", 9, "bold"))
        if self.step_seconds > 0:                     # preview-only playhead
            x = 2 + (self.elapsed / self.step_seconds) * (w - 4) / tot
            if x <= w - 2:
                c.create_line(x, top, x, h - 6, fill="#ffffff")

    # -- MCS notation (the .MCS target's preview) ----------------------------
    # Drawn from the ENCODED file's own records: each entry carries the exact
    # vertical staff position (v: top staff lines at 12/14/16/18/20, bottom at
    # 32..40 — calibrated against the encoder) and x-slot (8px columns, 24 per
    # measure) that Music Construction Set renders. Symbols: notes 0x00-0x05
    # (whole..32nd via _note_value), beamed 0x14-0x18, rests 0x07-0x0C,
    # accidentals 0x0E-0x10, dot 0x11, 8va 0x12, ties 0x13/0x19.
    _MEASURE_SLOTS = 24

    def _draw_notation(self, c, w, h) -> None:
        from ..mcs import reader as R
        if not self.staves:
            c.create_text(w / 2, h / 2, text="(no encoded song)",
                          fill="#000000")
            return
        c.configure(bg="#ffffff")                     # MCS: black on white
        vh = h / 46.0                                 # half-line step (v unit)
        slot_px = 26.0                                # one x-slot
        meas_px = self._MEASURE_SLOTS * slot_px
        tick = self.elapsed / max(1e-6, self.step_seconds)
        # scroll so the playhead sits at 1/3 of the window
        play_x = 40 + (tick / self.bar_ticks) * meas_px
        xoff = max(0.0, play_x - w / 3)
        nmeas = max(len(st) - 1 for st in self.staves)
        first = max(0, int(xoff // meas_px))
        last = min(nmeas, int((xoff + w) // meas_px) + 1)
        for line_v in (12, 14, 16, 18, 20, 32, 34, 36, 38, 40):   # staff lines
            y = line_v * vh
            c.create_line(0, y, w, y, fill="#000000")
        c.create_text(12, 16 * vh, text="𝄞", fill="#000000",
                      font=("Times", int(vh * 6)))
        c.create_text(12, 36 * vh, text="𝄢", fill="#000000",
                      font=("Times", int(vh * 5)))
        for m in range(first, last):                  # barlines + entries
            mx = 40 + m * meas_px - xoff
            c.create_line(mx, 12 * vh, mx, 20 * vh, fill="#000000")
            c.create_line(mx, 32 * vh, mx, 40 * vh, fill="#000000")
            for st in self.staves:
                if m + 1 >= len(st):
                    continue
                for b0, b1 in st[m + 1].entries:
                    self._draw_glyph(c, R, b0, b1, mx, vh, slot_px)
        c.create_line(play_x - xoff, 10 * vh, play_x - xoff, 42 * vh,
                      fill="#ff8f1f", width=2)        # the playhead

    def _draw_glyph(self, c, R, b0, b1, mx, vh, slot_px) -> None:
        sym = R.symbol(b0)
        v = R.vertical(b0, b1)
        x = mx + R.x_slot(b1) * slot_px + slot_px / 2
        y = v * vh
        kind, val = R._note_value(sym)
        ink = "#000000"
        if kind == "note":
            r = vh * 0.95                             # notehead half-height
            open_head = val >= 4                      # half/whole are open
            c.create_oval(x - r * 1.3, y - r, x + r * 1.3, y + r,
                          outline=ink, width=2,
                          fill="" if open_head else ink)
            if val <= 4:                              # stem (whole has none)
                up = v >= 16 if v <= 20 else v >= 36
                sy = y - vh * 7 if up else y + vh * 7
                sx = x + r * 1.3 if up else x - r * 1.3
                c.create_line(sx, y, sx, sy, fill=ink, width=2)
                flags = max(0, 3 - val) if val <= 2 else 0   # 8th=1..32nd=3
                for f in range(flags):
                    fy = sy + (f * vh * 1.2) * (1 if up else -1)
                    c.create_line(sx, fy, sx + vh * 1.6,
                                  fy + vh * 1.4 * (1 if up else -1),
                                  fill=ink, width=2)
            # ledger lines outside the staves (short strokes through the head)
            ledgers = []
            if v < 12:                                # above the top staff
                ledgers = range(v + (v & 1), 11, 2)
            elif 20 < v <= 26:                        # below the top staff
                ledgers = range(22, v + 1, 2)
            elif 26 < v < 32:                         # above the bottom staff
                ledgers = range(v + (v & 1), 31, 2)
            elif v > 40:                              # below the bottom staff
                ledgers = range(42, v + 1, 2)
            for lv_ in ledgers:
                ly = lv_ * vh
                c.create_line(x - r * 2.2, ly, x + r * 2.2, ly, fill=ink)
        elif kind == "rest":
            glyph = ("𝄾", "𝄽", "𝄼", "𝄼", "𝄻", "𝄻")[min(5, val)]
            c.create_text(x, y, text=glyph, fill=ink, font=("Times", int(vh * 3)))
        elif sym == R.SYM_SHARP:
            c.create_text(x, y, text="♯", fill=ink, font=("Times", int(vh * 2.4)))
        elif sym == R.SYM_FLAT:
            c.create_text(x, y, text="♭", fill=ink, font=("Times", int(vh * 2.4)))
        elif sym == R.SYM_NATURAL:
            c.create_text(x, y, text="♮", fill=ink, font=("Times", int(vh * 2.4)))
        elif sym == R.SYM_DOT:
            c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=ink, outline=ink)
        elif sym == R.SYM_OCTAVA:
            c.create_text(x, y, text="8va", fill=ink, font=("Times", int(vh * 1.8)))
        elif sym in (R.SYM_TIE, R.SYM_TIE_BELOW):
            below = sym == R.SYM_TIE_BELOW
            c.create_arc(x - slot_px, y - vh * 2, x + slot_px, y + vh * 2,
                         start=0 if below else 180, extent=180,
                         style="arc", outline=ink)

    def _draw_scopegrid(self, c, w, h, lv, pr, x0, y0, x1, y1, line=True,
                        colors=None) -> None:
        cw, chh = (x1 - x0) / 2, (y1 - y0) / 2
        for k in range(4):
            cx0 = x0 + (k % 2) * cw
            cy0 = y0 + (k // 2) * chh
            color = (colors or _DOS_CH)[k]
            c.create_text(cx0 + 8, cy0 + 6, text=self.names[k], anchor="nw",
                          fill=color, font=("Consolas", 9))
            if k == 3 and lv[3] > 0.001:             # noise: jitter, not a square
                rng = np.random.default_rng(int(self.phase))
                ys = rng.uniform(-1, 1, 24)
                mid = cy0 + chh / 2
                pts = []
                for i, y in enumerate(ys):
                    pts.extend([cx0 + 8 + i * (cw - 16) / 23,
                                mid + y * chh * 0.3])
                c.create_line(*pts, fill=color)
            else:
                self._scope_trace(c, cx0 + 6, cy0 + 4, cx0 + cw - 6,
                                  cy0 + chh - 4, lv[k], pr[k], color, line)
            if not line:                             # block style: fill under trace
                pass
        grey = _DOS["grey"]
        c.create_line(x0 + 2, y0 + chh, x1 - 2, y0 + chh, fill=grey)

    def _draw_spectrum(self, c, w, h, sp, region=None, full=False) -> None:
        x0, y0, x1, y1 = region or (0, 0, w, h)
        n = len(sp)
        bw = (x1 - x0 - 16) / n
        for i in range(n):
            v = min(1.0, sp[i])
            self.spec[i % 18] = max(v, self.spec[i % 18] - 0.05)
            v = self.spec[i % 18]
            bx0 = x0 + 8 + i * bw + 1
            bx1 = x0 + 8 + (i + 1) * bw - 1
            top = y1 - 10 - (y1 - y0 - 24) * v
            color = (_DOS["green"] if v < 0.5 else
                     _DOS["yellow"] if v < 0.8 else _DOS["brightred"])
            c.create_rectangle(bx0, top, bx1, y1 - 10, fill=color, outline="")

    def _draw_vu(self, c, w, h, lv, x0, y0, x1, y1, big=False) -> None:
        n = 4
        row_h = (y1 - y0) / n
        for i in range(n):
            self.vu[i] = max(lv[i] if i < len(lv) else 0.0, self.vu[i] * 0.82)
            self.vupk[i] = max(self.vu[i], self.vupk[i] - 0.01)
            ry = y0 + i * row_h
            c.create_text(x0 + 10, ry + row_h / 2, text=self.names[i], anchor="w",
                          fill=_DOS_CH[i], font=("Consolas", 10 if big else 9))
            bx0 = x0 + 50
            bx1 = x1 - 16
            span = bx1 - bx0
            fill = bx0 + span * self.vu[i]
            for frac, color in ((0.65, _DOS["green"]), (0.85, _DOS["yellow"]),
                                (1.0, _DOS["brightred"])):
                s0 = bx0 + span * (0 if frac == 0.65 else (0.65 if frac == 0.85 else 0.85))
                s1 = min(fill, bx0 + span * frac)
                if s1 > s0:
                    c.create_rectangle(s0, ry + row_h * 0.3, s1, ry + row_h * 0.7,
                                       fill=color, outline="")
            px = bx0 + span * self.vupk[i]
            c.create_line(px, ry + row_h * 0.25, px, ry + row_h * 0.75,
                          fill="#ffffff", width=2)


def voice_periods(song) -> List[float]:
    """Relative scope periods per track (0..1, lower = higher pitch) from each
    track's median pitch — drives the DOS replica's wave tightness."""
    out = []
    for t in song.tracks:
        pitches = [n.midi_note for n in t.notes if not n.is_rest]
        if not pitches:
            out.append(0.3)
        else:
            med = sorted(pitches)[len(pitches) // 2]
            out.append(max(0.05, min(1.0, (96 - med) / 60.0)))
    return out
