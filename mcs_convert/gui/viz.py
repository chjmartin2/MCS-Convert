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


def _window(parent, title: str, minsize=(360, 240), on_close=None):
    """A visualization Toplevel. `on_close` runs when the user closes it (the
    export preview passes its stop, so shutting the window stops the audio)."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=_BG)
    win.minsize(*minsize)
    if on_close is not None:
        def _closed():
            try:
                on_close()
            finally:
                win.destroy()
        win.protocol("WM_DELETE_WINDOW", _closed)
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
                 names: Optional[List[str]] = None, on_close=None) -> None:
        self.win = _window(parent, f"DOS preview — {style}", (640, 400),
                           on_close=on_close)
        # the DOS screen at native size: 320x200 blitted 2x, and 80x25 text on
        # the same 8x16 cell grid -- both are exactly 640x400, so the window is
        # fixed there and nothing is stretched or approximated
        self.canvas = tk.Canvas(self.win, bg="#000000", highlightthickness=0,
                                width=640, height=400)
        self.canvas.pack()
        self.win.resizable(False, False)
        self.style = style
        self.names = (names or ["P1", "P2", "Tr", "Nz"])[:4]
        self.vu = [0.0] * 4
        self.vupk = [0.0] * 4
        self.spec = [0.0] * 18
        self.phase = 0.0
        self.wave = "square"             # the waveform this build sounds
        self.song = None                 # for the static poster (set_song)
        self._poster = None              # the baked CGA framebuffer, cached
        self._poster_for = None
        self._img = None                 # the blitted frame (kept referenced)
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

    def set_wave(self, wave: str) -> None:
        """The waveform the chosen target sounds — the traces draw its contour."""
        self.wave = wave or "square"

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
        """The canvas is pinned to the DOS screen's real size (640x400), so the
        drawing space never depends on how the window happens to be sized."""
        return (self.canvas, self.NATIVE_W * self.ZOOM, self.NATIVE_H * self.ZOOM)

    def _scope_trace(self, c, x0, y0, x1, y1, level: float, period: float,
                     color: str, line: bool) -> None:
        """One channel's scrolling trace, drawn with the contour of the waveform
        this build actually sounds (self.wave) — the same shape the .COM's
        scopes bake in. A square still draws as the hard two-level trace; a
        sine rolls, and a narrow pulse shows its true duty."""
        from ..dosplayer import _wave_value
        mid = (y0 + y1) / 2
        amp = (y1 - y0) * 0.32
        if level <= 0.001:
            c.create_line(x0, mid, x1, mid, fill=color)
            return
        cycle = max(12.0, period * 52.0)             # on-screen FULL cycle
        step = max(1.0, cycle / 32.0)                # sample the contour finely
        shift = (self.phase * 60.0) % cycle
        pts = []
        x = x0
        while x <= x1:
            ph = (((x - x0) + shift) % cycle) / cycle
            pts.extend([x, mid - _wave_value(self.wave, ph) * amp])
            x += step
        if len(pts) >= 4:
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
        elif style.startswith("line trace") or style.startswith("block")                 or style.startswith("line scopes"):
            self._draw_textgrid(c, w, h, lv, pr, style)   # a real 80x25 grid
        elif style.startswith("Tandy") or style.startswith("VGA"):
            self._draw_graphics(c, w, h, lv, pr,
                                tandy=style.startswith("Tandy"))
        elif style.startswith("static"):
            self._draw_poster(c, w, h)
        elif style.startswith("MCS"):
            self._draw_notation(c, w, h)
        else:
            self._draw_textgrid(c, w, h, lv, pr, style)

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

    # -- the text scopes, drawn as a real 80x25 character grid ---------------
    #: CP437 glyphs the text engines write, in the order the caps ladder uses.
    _G_FULL, _G_QUARTER, _G_THREE = "█", "░", "▓"
    _G_HALF_UP, _G_HALF_DOWN = "▄", "▀"

    def _draw_textgrid(self, c, w, h, lv, pr, style: str) -> None:
        """The text scopes as DOS actually shows them: an 80x25 CHARACTER grid
        with the engine's own glyphs, bands and colours — not smooth vector
        lines. text 1 fills blocks from each band's centre and caps the column
        with the sub-row ladder (░ ▄/▀ ▓); the line styles trace with box
        drawing; every cell lands on the character grid."""
        from ..dosplayer import _TAMP, _TATTR, _TCEN, _wave_value
        cols, rows = 80, 25
        cw, ch_ = self.CELL_W, self.CELL_H        # the real VGA text cell
        font = ("Consolas", -self.CELL_H)         # -N = pixel height, not points
        attr = {0x0E: "#ffff55", 0x04: "#aa0000", 0x01: "#0000aa",
                0x0A: "#55ff55", 0x0F: "#ffffff"}

        def cell(x, y, glyph, ink):
            c.create_text((x + 0.5) * cw, (y + 0.5) * ch_, text=glyph,
                          fill=ink, font=font)

        blocks = style.startswith("block")            # text 1 fills; others trace
        scroll = self.phase * 2.0
        msum = [0.0] * cols
        for k in range(4):
            cen, ink = _TCEN[k], attr[_TATTR[k]]
            if lv[k] <= 0.001:                        # silent: a flat centre line
                for x in range(cols):
                    cell(x, cen, "─" if not blocks else self._G_FULL, ink)
                continue
            cycle = max(4.0, pr[k] * 60.0)
            prev = None
            for x in range(cols):
                v = _wave_value(self.wave, ((x + scroll) % cycle) / cycle)
                msum[x] += 1.0 if v >= 0 else -1.0
                if blocks:
                    # quarter-row height, then the same cap ladder the .COM bakes
                    q = int(round(-v * _TAMP * 4))
                    up, n = q < 0, abs(q)
                    full, rem = n >> 2, n & 3
                    for r in range(1, full + 1):
                        cell(x, cen - r if up else cen + r, self._G_FULL, ink)
                    cell(x, cen, self._G_FULL, ink)
                    if rem:
                        cap = (self._G_HALF_UP if up else self._G_HALF_DOWN) \
                            if rem == 2 else (self._G_QUARTER if rem == 1
                                              else self._G_THREE)
                        cell(x, (cen - full - 1) if up else (cen + full + 1),
                             cap, ink)
                else:
                    r = cen - int(round(v * _TAMP))
                    if prev is None or prev == r:
                        cell(x, r, "─", ink)
                    else:
                        # a step: a proper connected riser — the corner at the
                        # row we LEAVE turns down/up out of the incoming line,
                        # the one we ARRIVE at turns back out to the right
                        down = r > prev
                        for yy in range(min(prev, r), max(prev, r) + 1):
                            if yy == prev:
                                glyph = "┐" if down else "┘"
                            elif yy == r:
                                glyph = "└" if down else "┌"
                            else:
                                glyph = "│"
                            cell(x, yy, glyph, ink)
                    prev = r
        # the master band, and the labels DOS prints down the left edge
        mcen, mink = _TCEN[4], attr[0x0F]
        for x in range(cols):
            r = mcen - int(round(max(-2, min(2, msum[x] / 1.5))))
            cell(x, r, self._G_FULL if blocks else "─", mink)
        for k, name in enumerate(self.names[:4]):
            for i, chx in enumerate(name[:2]):
                cell(i, _TCEN[k], chx, attr[_TATTR[k]])

    # -- native-resolution framebuffer blitting -------------------------------
    # The graphics screens are drawn into a REAL 320x200 palette-indexed buffer,
    # exactly as the .COM builds one, then blitted as a single image scaled 2x
    # to 640x400. That makes the preview pixel-for-pixel what DOS shows —
    # canvas vectors could only ever approximate it (and 64000 rectangles would
    # be far too slow). Text screens use the true 80x25 / 8x16 cell grid, which
    # is the same 640x400.
    NATIVE_W, NATIVE_H, ZOOM = 320, 200, 2
    CELL_W, CELL_H = 8, 16                       # VGA 80x25 text cell = 640x400

    def _blit(self, c, px, palette) -> None:
        """Show a 320x200 index buffer through `palette` at 2x — one image."""
        row = bytearray()
        rgb = [bytes.fromhex(p[1:]) for p in palette]
        for y in range(self.NATIVE_H):
            line = px[y]
            for x in range(self.NATIVE_W):
                row += rgb[line[x]]
        img = tk.PhotoImage(
            data=b"P6 %d %d 255 " % (self.NATIVE_W, self.NATIVE_H) + bytes(row)
        ).zoom(self.ZOOM, self.ZOOM)
        self._img = img                          # keep a reference alive
        c.create_image(0, 0, image=img, anchor="nw")

    def _plot_v(self, px, x_col, y0, y1, ink) -> None:
        """The engine's vline: a column (2 pixels wide, as both graphics modes
        write it) filled from y0 to y1 inclusive."""
        if y0 > y1:
            y0, y1 = y1, y0
        x = x_col * 2
        for y in range(max(0, y0), min(self.NATIVE_H - 1, y1) + 1):
            row = px[y]
            if 0 <= x < self.NATIVE_W:
                row[x] = ink
            if 0 <= x + 1 < self.NATIVE_W:
                row[x + 1] = ink

    def _plot_frame(self, px, x0, x1, y0, y1, ink) -> None:
        for x in range(x0, x1 + 1):
            self._plot_v(px, x, y0, y0, ink)
            self._plot_v(px, x, y1, y1, ink)
        for y in (y0, y1):
            pass
        for y in range(y0, y1 + 1):
            for xc in (x0, x1):
                self._plot_v(px, xc, y, y, ink)

    # -- the graphics scopes, drawn to the .COM's OWN layout -----------------
    #: the 16-colour palette both graphics modes index into (Tandy mode 9 packs
    #: two of these per byte; VGA mode 13h uses them as plain indices)
    _PAL16 = ("#000000", "#0000aa", "#00aa00", "#00aaaa", "#aa0000", "#aa00aa",
              "#aa5500", "#aaaaaa", "#555555", "#5555ff", "#55ff55", "#55ffff",
              "#ff5555", "#ff55ff", "#ffff55", "#ffffff")

    def _draw_graphics(self, c, w, h, lv, pr, tandy: bool) -> None:
        """The mode-9 / mode-13h scope screen, rendered into a real 320x200
        framebuffer with the engine's OWN constants and drawing rules, then
        blitted at 2x. Pixel-for-pixel what the .COM puts on screen: the same
        bands, amplitude, 2-pixel columns, vline connections, noise shimmer,
        stepped master and white frames, in the mode's own palette."""
        from ..dosplayer import (_CH, _CH13, _CHW, _FRAMES, _GAMP,
                                 _MASTER_CEN_Y, _MASTER_K, _NOISE_CEN,
                                 _wave_value)
        chans = _CH if tandy else _CH13
        px = [bytearray(self.NATIVE_W) for _ in range(self.NATIVE_H)]
        idx = lambda packed: (packed >> 4) if tandy else packed
        scroll = self.phase * 2.0                    # _SCROLL_SPEED
        sums = [0.0] * _CHW
        for k in range(4):
            hi, lo, cen, packed, left = chans[k]
            ink = idx(packed)
            if k == 3:                               # noise: the LCG shimmer
                if lv[3] > 0.001:
                    seed = (int(self.phase) * 25173 + 13849) & 0xFFFF
                    for L in range(_CHW):
                        seed = (seed * 25173 + 13849) & 0xFFFF
                        y = _NOISE_CEN + ((seed >> 8) & 0x1F) - 16
                        self._plot_v(px, left + L, _NOISE_CEN, y, ink)
                else:
                    for L in range(_CHW):
                        self._plot_v(px, left + L, _NOISE_CEN, _NOISE_CEN, ink)
                continue
            if lv[k] <= 0.001:                       # silent: the centre line
                for L in range(_CHW):
                    self._plot_v(px, left + L, cen, cen, ink)
                continue
            cycle = max(4.0, pr[k] * 60.0)
            prev = cen
            for L in range(_CHW):
                v = _wave_value(self.wave, ((L + scroll) % cycle) / cycle)
                y = int(round(cen - v * _GAMP))
                self._plot_v(px, left + L, prev, y, ink)   # connect, like vline
                prev = y
                sums[L] += 1.0 if v >= 0 else -1.0
        prev = _MASTER_CEN_Y                         # the master, 2 columns wide
        for L in range(_CHW):
            y = int(round(_MASTER_CEN_Y - sums[L] * _MASTER_K))
            self._plot_v(px, 10 + 2 * L, prev, y, 15)
            self._plot_v(px, 10 + 2 * L + 1, prev, y, 15)
            prev = y
        for fr in _FRAMES:                           # the white frames, last
            self._plot_frame(px, fr[0], fr[1], fr[2], fr[3], 15)
        self._blit(c, px, self._PAL16)

    def _draw_poster(self, c, w, h) -> None:
        """The static screen: unpack the ACTUAL baked CGA framebuffer the .COM
        carries and show it. Not a redrawing — the very bytes DOS displays."""
        from ..dosplayer import _CGA_GREEN, _CGA_RED, _CGA_YELLOW, _render_static_poster
        if self.song is None:
            return
        if self._poster_for is not self.song:        # cache: it never changes
            self._poster_for = self.song
            self._poster = _render_static_poster(self.song)
        fb = self._poster
        px = [bytearray(self.NATIVE_W) for _ in range(self.NATIVE_H)]
        for y in range(self.NATIVE_H):               # even/odd interleaved planes
            base = (0x2000 if (y & 1) else 0) + (y >> 1) * 80
            row = px[y]
            for bx in range(80):
                b = fb[base + bx]
                x = bx * 4
                row[x] = (b >> 6) & 3
                row[x + 1] = (b >> 4) & 3
                row[x + 2] = (b >> 2) & 3
                row[x + 3] = b & 3
        # CGA mode 4, palette 0 + intensity: black / green / red / yellow
        self._blit(c, px, ("#000000", "#55ff55", "#ff5555", "#ffff55"))

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
