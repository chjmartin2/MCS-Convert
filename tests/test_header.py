import struct

import pytest

from mcs_convert.nsf.header import NSFHeader, HEADER_SIZE


def make_header(**over) -> bytes:
    buf = bytearray(HEADER_SIZE)
    buf[0:5] = b"NESM\x1a"
    buf[0x05] = over.get("version", 1)
    buf[0x06] = over.get("total", 3)
    buf[0x07] = over.get("start", 1)
    struct.pack_into("<HHH", buf, 0x08,
                     over.get("load", 0x8000),
                     over.get("init", 0x8003),
                     over.get("play", 0x8006))
    name = over.get("name", "Test Song").encode("latin-1")
    buf[0x0E:0x0E + len(name)] = name
    struct.pack_into("<H", buf, 0x6E, over.get("ntsc", 16666))
    buf[0x7A] = over.get("region", 0)
    buf[0x7B] = over.get("chips", 0)
    return bytes(buf)


def test_parses_basic_fields():
    h = NSFHeader.parse(make_header())
    assert h.song_name == "Test Song"
    assert h.total_songs == 3
    assert h.load_addr == 0x8000 and h.play_addr == 0x8006
    assert not h.is_pal
    assert abs(h.play_rate_hz - 60.0) < 0.2
    assert h.expansion_chips == []


def test_rejects_bad_magic():
    bad = bytearray(make_header())
    bad[0] = ord("X")
    with pytest.raises(ValueError):
        NSFHeader.parse(bytes(bad))


def test_too_short():
    with pytest.raises(ValueError):
        NSFHeader.parse(b"NESM\x1a")


def test_expansion_chip_and_pal_flags():
    h = NSFHeader.parse(make_header(region=0x01, chips=0x01))  # PAL + VRC6
    assert h.is_pal
    assert "VRC6" in h.expansion_chips
