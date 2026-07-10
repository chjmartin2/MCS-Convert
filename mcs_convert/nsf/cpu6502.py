"""MOS 6502 / RP2A03 CPU core — the engine that runs NSF player code.

To pull note data out of an NSF we must actually *run* the tune's player code: load
the program data at `load_addr`, JSR to `init_addr` once to set up song N, then JSR
to `play_addr` once per frame. The player writes to the APU registers ($4000-$4017)
as it goes; those writes are what we capture.

This is a table-driven interpreter of the 151 official opcodes (the 2A03 has no
decimal mode: the D flag exists but arithmetic ignores it, exactly like the NES).
Validated instruction-by-instruction against the `nestest` golden log (Nintendulator
trace) — see tests/test_cpu6502.py.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

# Status flags.
C, Z, I, D, B, U, V, N = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80

_SENTINEL = 0xFFFF          # fake JSR return target: RTS to here ends a call()


class MemoryBus:
    """64 KiB address space with a write hook for APU-register interception.

    NSF quirk: writes in $4000-$4017 hit the APU; $5FF8-$5FFF are bankswitch
    registers (when bankswitching is used). Everything else is plain RAM/ROM here.
    """

    def __init__(self, on_apu_write: Optional[Callable[[int, int], None]] = None):
        self.ram = bytearray(0x10000)
        self.on_apu_write = on_apu_write
        self.on_bank_write: Optional[Callable[[int, int], None]] = None

    def read(self, addr: int) -> int:
        return self.ram[addr & 0xFFFF]

    def write(self, addr: int, value: int) -> None:
        addr &= 0xFFFF
        value &= 0xFF
        if 0x4000 <= addr <= 0x4017 and self.on_apu_write is not None:
            self.on_apu_write(addr, value)
        if 0x5FF8 <= addr <= 0x5FFF and self.on_bank_write is not None:
            self.on_bank_write(addr, value)
            return
        self.ram[addr] = value

    def load(self, addr: int, data: bytes) -> None:
        end = min(0x10000, addr + len(data))
        self.ram[addr:end] = data[:end - addr]


class CPU6502:
    """Official-opcode 6502 interpreter over a MemoryBus."""

    def __init__(self, bus: MemoryBus):
        self.bus = bus
        self.a = self.x = self.y = 0
        self.sp = 0xFD
        self.pc = 0
        self.p = U | I
        self.cycles = 0
        if not _OPS:
            _build_ops()

    # ---- tiny helpers -----------------------------------------------------------
    def _rd(self, a: int) -> int:
        return self.bus.read(a)

    def _wr(self, a: int, v: int) -> None:
        self.bus.write(a, v)

    def _rd16(self, a: int) -> int:
        return self._rd(a) | (self._rd((a + 1) & 0xFFFF) << 8)

    def _push(self, v: int) -> None:
        self._wr(0x100 | self.sp, v & 0xFF)
        self.sp = (self.sp - 1) & 0xFF

    def _pull(self) -> int:
        self.sp = (self.sp + 1) & 0xFF
        return self._rd(0x100 | self.sp)

    def _nz(self, v: int) -> int:
        self.p = (self.p & ~(Z | N)) | (Z if v == 0 else 0) | (v & N)
        return v

    # ---- addressing modes: return (address, page_crossed) ------------------------
    def _imm(self):
        a = self.pc
        self.pc = (self.pc + 1) & 0xFFFF
        return a, False

    def _zp(self):
        a = self._rd(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return a, False

    def _zpx(self):
        a = (self._rd(self.pc) + self.x) & 0xFF
        self.pc = (self.pc + 1) & 0xFFFF
        return a, False

    def _zpy(self):
        a = (self._rd(self.pc) + self.y) & 0xFF
        self.pc = (self.pc + 1) & 0xFFFF
        return a, False

    def _abs(self):
        a = self._rd16(self.pc)
        self.pc = (self.pc + 2) & 0xFFFF
        return a, False

    def _abx(self):
        base = self._rd16(self.pc)
        self.pc = (self.pc + 2) & 0xFFFF
        a = (base + self.x) & 0xFFFF
        return a, (base & 0xFF00) != (a & 0xFF00)

    def _aby(self):
        base = self._rd16(self.pc)
        self.pc = (self.pc + 2) & 0xFFFF
        a = (base + self.y) & 0xFFFF
        return a, (base & 0xFF00) != (a & 0xFF00)

    def _izx(self):
        z = (self._rd(self.pc) + self.x) & 0xFF
        self.pc = (self.pc + 1) & 0xFFFF
        a = self._rd(z) | (self._rd((z + 1) & 0xFF) << 8)
        return a, False

    def _izy(self):
        z = self._rd(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        base = self._rd(z) | (self._rd((z + 1) & 0xFF) << 8)
        a = (base + self.y) & 0xFFFF
        return a, (base & 0xFF00) != (a & 0xFF00)

    # ---- execution ---------------------------------------------------------------
    def step(self) -> None:
        op = self._rd(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        _OPS[op](self)

    def call(self, addr: int, a: int = None, x: int = None,
             max_cycles: int = 2_000_000) -> int:
        """JSR to `addr` and run until the matching RTS. Returns cycles consumed."""
        if a is not None:
            self.a = a & 0xFF
        if x is not None:
            self.x = x & 0xFF
        self._push((_SENTINEL - 1) >> 8)
        self._push((_SENTINEL - 1) & 0xFF)
        self.pc = addr & 0xFFFF
        start = self.cycles
        while self.pc != _SENTINEL:
            self.step()
            if self.cycles - start > max_cycles:
                raise RuntimeError(f"runaway NSF code (> {max_cycles} cycles)")
        return self.cycles - start


# ---- opcode table (built once) -----------------------------------------------------
_OPS: List[Callable[[CPU6502], None]] = []


def _build_ops() -> None:
    ops: List[Callable[[CPU6502], None]] = [None] * 256

    def ill(cpu: CPU6502) -> None:
        raise RuntimeError(f"illegal opcode at {cpu.pc - 1:04X}")

    for i in range(256):
        ops[i] = ill

    modes = {"imm": CPU6502._imm, "zp": CPU6502._zp, "zpx": CPU6502._zpx,
             "zpy": CPU6502._zpy, "abs": CPU6502._abs, "abx": CPU6502._abx,
             "aby": CPU6502._aby, "izx": CPU6502._izx, "izy": CPU6502._izy}

    def rmw(code: int, mode: str, cycles: int, fn) -> None:
        get = modes[mode]

        def h(cpu: CPU6502, get=get, fn=fn, cycles=cycles) -> None:
            a, _ = get(cpu)
            cpu._wr(a, cpu._nz(fn(cpu, cpu._rd(a))))
            cpu.cycles += cycles
        ops[code] = h

    def read_op(code: int, mode: str, cycles: int, fn, extra: bool = True) -> None:
        get = modes[mode]

        def h(cpu: CPU6502, get=get, fn=fn, cycles=cycles, extra=extra) -> None:
            a, crossed = get(cpu)
            fn(cpu, cpu._rd(a))
            cpu.cycles += cycles + (1 if crossed and extra else 0)
        ops[code] = h

    def store_op(code: int, mode: str, cycles: int, reg: str) -> None:
        get = modes[mode]

        def h(cpu: CPU6502, get=get, reg=reg, cycles=cycles) -> None:
            a, _ = get(cpu)
            cpu._wr(a, getattr(cpu, reg))
            cpu.cycles += cycles
        ops[code] = h

    # loads / logic / arithmetic ----------------------------------------------------
    def lda(cpu, v): cpu.a = cpu._nz(v)
    def ldx(cpu, v): cpu.x = cpu._nz(v)
    def ldy(cpu, v): cpu.y = cpu._nz(v)
    def and_(cpu, v): cpu.a = cpu._nz(cpu.a & v)
    def ora(cpu, v): cpu.a = cpu._nz(cpu.a | v)
    def eor(cpu, v): cpu.a = cpu._nz(cpu.a ^ v)

    def adc(cpu, v):
        s = cpu.a + v + (cpu.p & C)
        cpu.p = (cpu.p & ~(C | V)) | (C if s > 0xFF else 0) | \
                (V if (~(cpu.a ^ v) & (cpu.a ^ s)) & 0x80 else 0)
        cpu.a = cpu._nz(s & 0xFF)

    def sbc(cpu, v): adc(cpu, v ^ 0xFF)

    def cmp_reg(reg):
        def f(cpu, v, reg=reg):
            r = getattr(cpu, reg)
            d = (r - v) & 0xFF
            cpu.p = (cpu.p & ~C) | (C if r >= v else 0)
            cpu._nz(d)
        return f

    def bit(cpu, v):
        cpu.p = (cpu.p & ~(Z | V | N)) | (0 if cpu.a & v else Z) | (v & (V | N))

    for code, mode, cyc in ((0xA9, "imm", 2), (0xA5, "zp", 3), (0xB5, "zpx", 4),
                            (0xAD, "abs", 4), (0xBD, "abx", 4), (0xB9, "aby", 4),
                            (0xA1, "izx", 6), (0xB1, "izy", 5)):
        read_op(code, mode, cyc, lda)
    for code, mode, cyc in ((0xA2, "imm", 2), (0xA6, "zp", 3), (0xB6, "zpy", 4),
                            (0xAE, "abs", 4), (0xBE, "aby", 4)):
        read_op(code, mode, cyc, ldx)
    for code, mode, cyc in ((0xA0, "imm", 2), (0xA4, "zp", 3), (0xB4, "zpx", 4),
                            (0xAC, "abs", 4), (0xBC, "abx", 4)):
        read_op(code, mode, cyc, ldy)
    for fn, table in ((and_, ((0x29, "imm", 2), (0x25, "zp", 3), (0x35, "zpx", 4),
                              (0x2D, "abs", 4), (0x3D, "abx", 4), (0x39, "aby", 4),
                              (0x21, "izx", 6), (0x31, "izy", 5))),
                      (ora, ((0x09, "imm", 2), (0x05, "zp", 3), (0x15, "zpx", 4),
                             (0x0D, "abs", 4), (0x1D, "abx", 4), (0x19, "aby", 4),
                             (0x01, "izx", 6), (0x11, "izy", 5))),
                      (eor, ((0x49, "imm", 2), (0x45, "zp", 3), (0x55, "zpx", 4),
                             (0x4D, "abs", 4), (0x5D, "abx", 4), (0x59, "aby", 4),
                             (0x41, "izx", 6), (0x51, "izy", 5))),
                      (adc, ((0x69, "imm", 2), (0x65, "zp", 3), (0x75, "zpx", 4),
                             (0x6D, "abs", 4), (0x7D, "abx", 4), (0x79, "aby", 4),
                             (0x61, "izx", 6), (0x71, "izy", 5))),
                      (sbc, ((0xE9, "imm", 2), (0xE5, "zp", 3), (0xF5, "zpx", 4),
                             (0xED, "abs", 4), (0xFD, "abx", 4), (0xF9, "aby", 4),
                             (0xE1, "izx", 6), (0xF1, "izy", 5))),
                      (cmp_reg("a"), ((0xC9, "imm", 2), (0xC5, "zp", 3),
                                      (0xD5, "zpx", 4), (0xCD, "abs", 4),
                                      (0xDD, "abx", 4), (0xD9, "aby", 4),
                                      (0xC1, "izx", 6), (0xD1, "izy", 5))),
                      (cmp_reg("x"), ((0xE0, "imm", 2), (0xE4, "zp", 3),
                                      (0xEC, "abs", 4))),
                      (cmp_reg("y"), ((0xC0, "imm", 2), (0xC4, "zp", 3),
                                      (0xCC, "abs", 4)))):
        for code, mode, cyc in table:
            read_op(code, mode, cyc, fn)
    read_op(0x24, "zp", 3, bit)
    read_op(0x2C, "abs", 4, bit)

    # stores --------------------------------------------------------------------
    for code, mode, cyc in ((0x85, "zp", 3), (0x95, "zpx", 4), (0x8D, "abs", 4),
                            (0x9D, "abx", 5), (0x99, "aby", 5), (0x81, "izx", 6),
                            (0x91, "izy", 6)):
        store_op(code, mode, cyc, "a")
    for code, mode, cyc in ((0x86, "zp", 3), (0x96, "zpy", 4), (0x8E, "abs", 4)):
        store_op(code, mode, cyc, "x")
    for code, mode, cyc in ((0x84, "zp", 3), (0x94, "zpx", 4), (0x8C, "abs", 4)):
        store_op(code, mode, cyc, "y")

    # read-modify-write -----------------------------------------------------------
    def asl_v(cpu, v):
        cpu.p = (cpu.p & ~C) | (C if v & 0x80 else 0)
        return (v << 1) & 0xFF

    def lsr_v(cpu, v):
        cpu.p = (cpu.p & ~C) | (v & C)
        return v >> 1

    def rol_v(cpu, v):
        c = cpu.p & C
        cpu.p = (cpu.p & ~C) | (C if v & 0x80 else 0)
        return ((v << 1) | c) & 0xFF

    def ror_v(cpu, v):
        c = cpu.p & C
        cpu.p = (cpu.p & ~C) | (v & C)
        return (v >> 1) | (c << 7)

    def inc_v(cpu, v): return (v + 1) & 0xFF
    def dec_v(cpu, v): return (v - 1) & 0xFF

    for fn, acc_code, table in (
            (asl_v, 0x0A, ((0x06, "zp", 5), (0x16, "zpx", 6), (0x0E, "abs", 6),
                           (0x1E, "abx", 7))),
            (lsr_v, 0x4A, ((0x46, "zp", 5), (0x56, "zpx", 6), (0x4E, "abs", 6),
                           (0x5E, "abx", 7))),
            (rol_v, 0x2A, ((0x26, "zp", 5), (0x36, "zpx", 6), (0x2E, "abs", 6),
                           (0x3E, "abx", 7))),
            (ror_v, 0x6A, ((0x66, "zp", 5), (0x76, "zpx", 6), (0x6E, "abs", 6),
                           (0x7E, "abx", 7)))):
        for code, mode, cyc in table:
            rmw(code, mode, cyc, fn)

        def acc_h(cpu: CPU6502, fn=fn) -> None:
            cpu.a = cpu._nz(fn(cpu, cpu.a))
            cpu.cycles += 2
        ops[acc_code] = acc_h
    for fn, table in ((inc_v, ((0xE6, "zp", 5), (0xF6, "zpx", 6), (0xEE, "abs", 6),
                               (0xFE, "abx", 7))),
                      (dec_v, ((0xC6, "zp", 5), (0xD6, "zpx", 6), (0xCE, "abs", 6),
                               (0xDE, "abx", 7)))):
        for code, mode, cyc in table:
            rmw(code, mode, cyc, fn)

    # register / flag one-byte ops ---------------------------------------------------
    def simple(code: int, cycles: int, fn) -> None:
        def h(cpu: CPU6502, fn=fn, cycles=cycles) -> None:
            fn(cpu)
            cpu.cycles += cycles
        ops[code] = h

    simple(0xAA, 2, lambda c: setattr(c, "x", c._nz(c.a)))          # TAX
    simple(0xA8, 2, lambda c: setattr(c, "y", c._nz(c.a)))          # TAY
    simple(0x8A, 2, lambda c: setattr(c, "a", c._nz(c.x)))          # TXA
    simple(0x98, 2, lambda c: setattr(c, "a", c._nz(c.y)))          # TYA
    simple(0xBA, 2, lambda c: setattr(c, "x", c._nz(c.sp)))         # TSX
    simple(0x9A, 2, lambda c: setattr(c, "sp", c.x))                # TXS
    simple(0xE8, 2, lambda c: setattr(c, "x", c._nz((c.x + 1) & 0xFF)))   # INX
    simple(0xC8, 2, lambda c: setattr(c, "y", c._nz((c.y + 1) & 0xFF)))   # INY
    simple(0xCA, 2, lambda c: setattr(c, "x", c._nz((c.x - 1) & 0xFF)))   # DEX
    simple(0x88, 2, lambda c: setattr(c, "y", c._nz((c.y - 1) & 0xFF)))   # DEY
    simple(0xEA, 2, lambda c: None)                                 # NOP
    for code, flag, val in ((0x18, C, 0), (0x38, C, 1), (0x58, I, 0), (0x78, I, 1),
                            (0xB8, V, 0), (0xD8, D, 0), (0xF8, D, 1)):
        simple(code, 2, (lambda flag, val: lambda c: setattr(
            c, "p", (c.p | flag) if val else (c.p & ~flag)))(flag, val))

    simple(0x48, 3, lambda c: c._push(c.a))                         # PHA
    simple(0x08, 3, lambda c: c._push(c.p | B | U))                 # PHP
    simple(0x68, 4, lambda c: setattr(c, "a", c._nz(c._pull())))    # PLA
    simple(0x28, 4, lambda c: setattr(c, "p", (c._pull() | U) & ~B))  # PLP

    # jumps / branches / interrupts ---------------------------------------------------
    def jmp_abs(cpu: CPU6502) -> None:
        cpu.pc = cpu._rd16(cpu.pc)
        cpu.cycles += 3
    ops[0x4C] = jmp_abs

    def jmp_ind(cpu: CPU6502) -> None:
        a = cpu._rd16(cpu.pc)
        # 6502 bug: the pointer high byte is fetched without carrying into the page
        lo = cpu._rd(a)
        hi = cpu._rd((a & 0xFF00) | ((a + 1) & 0xFF))
        cpu.pc = lo | (hi << 8)
        cpu.cycles += 5
    ops[0x6C] = jmp_ind

    def jsr(cpu: CPU6502) -> None:
        target = cpu._rd16(cpu.pc)
        ret = (cpu.pc + 1) & 0xFFFF
        cpu._push(ret >> 8)
        cpu._push(ret & 0xFF)
        cpu.pc = target
        cpu.cycles += 6
    ops[0x20] = jsr

    def rts(cpu: CPU6502) -> None:
        lo = cpu._pull()
        hi = cpu._pull()
        cpu.pc = ((lo | (hi << 8)) + 1) & 0xFFFF
        cpu.cycles += 6
    ops[0x60] = rts

    def rti(cpu: CPU6502) -> None:
        cpu.p = (cpu._pull() | U) & ~B
        lo = cpu._pull()
        hi = cpu._pull()
        cpu.pc = lo | (hi << 8)
        cpu.cycles += 6
    ops[0x40] = rti

    def brk(cpu: CPU6502) -> None:
        ret = (cpu.pc + 1) & 0xFFFF
        cpu._push(ret >> 8)
        cpu._push(ret & 0xFF)
        cpu._push(cpu.p | B | U)
        cpu.p |= I
        cpu.pc = cpu._rd16(0xFFFE)
        cpu.cycles += 7
    ops[0x00] = brk

    def branch(code: int, flag: int, want: int) -> None:
        def h(cpu: CPU6502, flag=flag, want=want) -> None:
            off = cpu._rd(cpu.pc)
            cpu.pc = (cpu.pc + 1) & 0xFFFF
            cpu.cycles += 2
            if bool(cpu.p & flag) == bool(want):
                target = (cpu.pc + (off - 256 if off & 0x80 else off)) & 0xFFFF
                cpu.cycles += 1 + (1 if (target & 0xFF00) != (cpu.pc & 0xFF00) else 0)
                cpu.pc = target
        ops[code] = h

    branch(0x10, N, 0)   # BPL
    branch(0x30, N, 1)   # BMI
    branch(0x50, V, 0)   # BVC
    branch(0x70, V, 1)   # BVS
    branch(0x90, C, 0)   # BCC
    branch(0xB0, C, 1)   # BCS
    branch(0xD0, Z, 0)   # BNE
    branch(0xF0, Z, 1)   # BEQ

    _OPS.extend(ops)
