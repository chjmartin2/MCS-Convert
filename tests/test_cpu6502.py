"""Tests for the 6502 core: a hand-assembled smoke program (always runs) and the
nestest golden-log comparison (the community's reference trace, instruction by
instruction, all 151 official opcodes)."""

import gzip
import os
import re

import pytest

from mcs_convert.nsf.cpu6502 import CPU6502, MemoryBus

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def test_smoke_program():
    # LDA #$05; STA $10; LDA #$FB; CLC; ADC $10; STA $11; JSR sub; RTS-to-sentinel
    # sub: INX; RTS
    bus = MemoryBus()
    bus.load(0x8000, bytes([
        0xA9, 0x05,        # LDA #$05
        0x85, 0x10,        # STA $10
        0xA9, 0xFB,        # LDA #$FB
        0x18,              # CLC
        0x65, 0x10,        # ADC $10   -> 0x00, carry set, zero set
        0x85, 0x11,        # STA $11
        0x20, 0x10, 0x80,  # JSR $8010
        0x60,              # RTS (ends the call)
    ]))
    bus.load(0x8010, bytes([0xE8, 0x60]))          # INX; RTS
    cpu = CPU6502(bus)
    cpu.call(0x8000, a=0, x=7)
    assert bus.read(0x10) == 5 and bus.read(0x11) == 0
    assert cpu.a == 0 and cpu.x == 8
    assert cpu.p & 0x01                  # carry from the ADC survived
    assert not cpu.p & 0x02              # ...but Z was rewritten by INX (x=8)


def test_apu_write_hook():
    writes = []
    bus = MemoryBus(on_apu_write=lambda a, v: writes.append((a, v)))
    bus.load(0x8000, bytes([0xA9, 0x42, 0x8D, 0x02, 0x40, 0x60]))  # LDA/STA $4002/RTS
    CPU6502(bus).call(0x8000)
    assert writes == [(0x4002, 0x42)]


def test_nestest_golden_log():
    rom_path = os.path.join(FIXTURES, "nestest.nes")
    log_path = os.path.join(FIXTURES, "nestest.log.gz")
    if not (os.path.exists(rom_path) and os.path.exists(log_path)):
        pytest.skip("nestest fixtures not present")
    prg = open(rom_path, "rb").read()[16:16 + 16384]
    bus = MemoryBus()
    bus.load(0x8000, prg)
    bus.load(0xC000, prg)
    cpu = CPU6502(bus)
    cpu.pc, cpu.sp, cpu.p = 0xC000, 0xFD, 0x24

    line_re = re.compile(rb"^([0-9A-F]{4}).*A:([0-9A-F]{2}) X:([0-9A-F]{2}) "
                         rb"Y:([0-9A-F]{2}) P:([0-9A-F]{2}) SP:([0-9A-F]{2})")
    matched = 0
    with gzip.open(log_path) as fh:
        for line in fh:
            m = line_re.match(line)
            if not m:
                continue
            want = tuple(int(m.group(k), 16) for k in range(1, 7))
            got = (cpu.pc, cpu.a, cpu.x, cpu.y, cpu.p, cpu.sp)
            assert got == want, (f"diverged after {matched} instructions: "
                                 f"want {want}, got {got}")
            matched += 1
            try:
                cpu.step()
            except RuntimeError:
                break                   # the log's unofficial-opcode section
    assert matched >= 5000              # every official opcode exercised
    assert bus.read(2) == 0 and bus.read(3) == 0    # nestest's own error bytes
