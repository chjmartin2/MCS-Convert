"""Target-mode linting for the tracker: which cells a chosen output target
can actually express, so RCTracker can highlight the ones that won't survive
(lint + snap -- never blocks entry). Pure logic; the UI just colours what
`lint_cell` flags.

The modes correspond to the export targets. "free" is the tracker's own full
capability (everything is fine). The rest describe what that hardware/format
keeps when you export to it, mirroring retrack.py + the .MCS encoder.
"""

from __future__ import annotations

from typing import List, Optional

from .rct import (FX_ARP, FX_BREAK, FX_CUT, FX_DELAY, FX_NONE, FX_ORNAMENT,
                  FX_PORTA, FX_SLIDE_DOWN, FX_SLIDE_UP, FX_SPEED, FX_VIBRATO,
                  FX_VOLSLIDE, RctCell)

#: selectable modes (order = the UI cycle)
MODES = ("free", "mcs", "tandy", "4voice", "1voice", "sb")

MODE_LABELS = {
    "free": "Free (RCT)",
    "mcs": "MCS notation",
    "tandy": "Tandy / PCjr",
    "4voice": "4-voice speaker",
    "1voice": "1-voice speaker",
    "sb": "SoundBlaster",
}

# per mode: which effects survive, whether per-note volume matters, whether
# per-note waveform matters, and whether the tempo is snapped to the MCS grid.
_ALL_FX = {FX_ARP, FX_SLIDE_UP, FX_SLIDE_DOWN, FX_PORTA, FX_VIBRATO,
           FX_VOLSLIDE, FX_CUT, FX_DELAY, FX_ORNAMENT, FX_SPEED, FX_BREAK}
_STRUCT_FX = {FX_SPEED, FX_BREAK}                    # always fine (song structure)

_PROFILE = {
    # MCS is plain notation: no runtime effects, no volume, square only, and
    # the tempo is one of the ten MCS bytes.
    "mcs": dict(fx=_STRUCT_FX, volume=False, waveform=False, tempo_snap=True),
    # Tandy has real 4-bit volume; the SN can't do our pitch effects on the
    # baked stream cleanly, but arps/slides/vibrato DO reach it (retunes), so
    # allow the pitch set; ornaments too. No per-note waveform (squares+noise).
    "tandy": dict(fx=_ALL_FX - {FX_ORNAMENT} | {FX_ORNAMENT}, volume=True,
                  waveform=False, tempo_snap=False),
    # 4-voice speaker: 1-bit, so no volume; all pitch effects reach it.
    "4voice": dict(fx=_ALL_FX, volume=False, waveform=False, tempo_snap=False),
    # 1-voice: monophonic beeper (arps become the chord cycler); no volume,
    # no waveform; pitch effects on whatever voice is sounding.
    "1voice": dict(fx=_ALL_FX, volume=False, waveform=False, tempo_snap=False),
    # SoundBlaster keeps waveform AND volume (real DAC / FM); all effects.
    "sb": dict(fx=_ALL_FX, volume=True, waveform=True, tempo_snap=False),
}


def tempo_is_snapped(mode: str) -> bool:
    return _PROFILE.get(mode, {}).get("tempo_snap", False)


def lint_cell(cell: RctCell, mode: str, inst_wave: str = "square") -> List[str]:
    """Reasons this cell won't fully survive export to `mode` (empty = fine).
    `inst_wave` is the cell's effective waveform (for the waveform check)."""
    if mode == "free" or mode not in _PROFILE:
        return []
    prof = _PROFILE[mode]
    issues: List[str] = []
    if cell.fx and cell.fx not in prof["fx"]:
        issues.append("effect dropped")
    if cell.vol and not prof["volume"]:
        issues.append("volume ignored")
    if (cell.note and cell.note < 97 and not prof["waveform"]
            and inst_wave not in ("square", "")):
        issues.append("waveform -> square")
    return issues


def cell_ok(cell: RctCell, mode: str, inst_wave: str = "square") -> bool:
    return not lint_cell(cell, mode, inst_wave)
