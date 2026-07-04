"""MOS 6502 / RP2A03 CPU core — SKELETON.

To pull note data out of an NSF we must actually *run* the tune's player code: load the
program data at `load_addr`, JSR to `init_addr` once to set up song N, then JSR to
`play_addr` once per frame. The player writes to the APU registers ($4000-$4017) as it
goes; those writes are what we capture.

The NSF player runs on a bare 2A03 (a 6502 with no decimal mode). We need a correct,
cycle-countable core with a memory bus we can intercept so APU writes are logged.

This file defines the interface. The instruction-decode core is the main remaining work
on the input side — see docs/architecture.md. A pure-Python 6502 (e.g. `py65`) could be
dropped in behind `MemoryBus` instead of hand-rolling one.
"""

from __future__ import annotations

from typing import Callable, Optional


class MemoryBus:
    """64 KiB address space with a write hook for APU-register interception.

    NSF quirk: reads/writes in $4000-$401F hit the APU; $5FF8-$5FFF are bankswitch
    registers (when bankswitching is used). Everything else is plain RAM/ROM here.
    """

    def __init__(self, on_apu_write: Optional[Callable[[int, int], None]] = None):
        self.ram = bytearray(0x10000)
        self.on_apu_write = on_apu_write

    def read(self, addr: int) -> int:
        return self.ram[addr & 0xFFFF]

    def write(self, addr: int, value: int) -> None:
        addr &= 0xFFFF
        value &= 0xFF
        if 0x4000 <= addr <= 0x4017 and self.on_apu_write is not None:
            self.on_apu_write(addr, value)
        self.ram[addr] = value

    def load(self, addr: int, data: bytes) -> None:
        self.ram[addr:addr + len(data)] = data


class CPU6502:
    """Interface placeholder for the 6502 core."""

    def __init__(self, bus: MemoryBus):
        self.bus = bus
        self.a = self.x = self.y = 0
        self.sp = 0xFD
        self.pc = 0
        self.status = 0x24
        self.cycles = 0

    def reset(self, pc: int) -> None:
        self.pc = pc & 0xFFFF
        self.sp = 0xFD
        self.cycles = 0

    def run_until_rts(self, max_cycles: int = 5_000_000) -> int:
        """Execute from the current PC until the initial JSR frame returns (RTS underflow).

        Returns cycles consumed. NOT YET IMPLEMENTED.
        """
        raise NotImplementedError(
            "6502 instruction core not implemented yet - see docs/architecture.md. "
            "This is the primary remaining work on the NSF input side."
        )
