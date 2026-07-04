"""APU register model: decode the pitched channels from register writes.

We only track the three channels that carry pitch and map onto staff notation:

    pulse 1    $4000-$4003
    pulse 2    $4004-$4007
    triangle   $4008-$400B

Noise ($400C-$400F) and DMC ($4010-$4013) are ignored for now (unpitched / sampled).
$4015 is the channel enable/length-status register.

Each channel's 11-bit timer period comes from the low register (bits 0-7) and the high
register (bits 0-2). Volume/enable come from the first register of each pulse channel and
from $4015. This module is pure state: feed it writes, read out per-channel pitch/on state.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..pitch import pulse_period_to_freq, triangle_period_to_freq, freq_to_midi_int

# Register base offsets by channel index.
PULSE1, PULSE2, TRIANGLE = 0, 1, 2
_BASE = {PULSE1: 0x4000, PULSE2: 0x4004, TRIANGLE: 0x4008}
STATUS = 0x4015


@dataclass
class ChannelState:
    period: int = 0
    volume: int = 0        # pulse: 0-15 envelope/const; triangle: on/off proxy
    enabled: bool = False  # from $4015

    def freq(self, index: int) -> float:
        if index == TRIANGLE:
            return triangle_period_to_freq(self.period)
        return pulse_period_to_freq(self.period)

    def midi_note(self, index: int):
        return freq_to_midi_int(self.freq(index))

    def is_sounding(self, index: int) -> bool:
        """Best-effort 'is this channel audibly playing a pitch right now'."""
        if not self.enabled:
            return False
        if self.midi_note(index) is None:
            return False
        if index == TRIANGLE:
            return self.period >= 2
        return self.volume > 0


class APUState:
    """Accumulates register writes into current per-channel state."""

    def __init__(self) -> None:
        self.channels = {
            PULSE1: ChannelState(),
            PULSE2: ChannelState(),
            TRIANGLE: ChannelState(),
        }

    def write(self, addr: int, value: int) -> None:
        value &= 0xFF
        if addr == STATUS:
            self.channels[PULSE1].enabled = bool(value & 0x01)
            self.channels[PULSE2].enabled = bool(value & 0x02)
            self.channels[TRIANGLE].enabled = bool(value & 0x04)
            return
        for index, base in _BASE.items():
            reg = addr - base
            if reg == 0 and index != TRIANGLE:      # $4000/$4004: duty + volume/env
                self.channels[index].volume = value & 0x0F
            elif reg == 2:                          # timer low
                ch = self.channels[index]
                ch.period = (ch.period & 0x0700) | value
            elif reg == 3:                          # timer high (+ length load)
                ch = self.channels[index]
                ch.period = (ch.period & 0x00FF) | ((value & 0x07) << 8)
                if index == TRIANGLE:
                    ch.volume = 15  # triangle has no volume; treat a write as "keyed"
            break
