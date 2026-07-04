"""NSF (NES Sound Format) header parsing.

Layout of the classic 128-byte NESM header (offsets in hex):

    00  5   magic: "NESM" + 0x1A
    05  1   version number
    06  1   total number of songs
    07  1   starting song (1-based)
    08  2   load address  (little-endian)
    0A  2   init address
    0C  2   play address
    0E  32  song name       (NUL-padded ASCII)
    2E  32  artist
    4E  32  copyright
    6E  2   NTSC play speed  (microseconds per play call, LE)
    70  8   bankswitch init values (all zero => no bankswitching)
    78  2   PAL play speed
    7A  1   PAL/NTSC flags: bit0 PAL, bit1 dual
    7B  1   expansion sound chip flags
    7C  4   NSF2 flags + program-data length (reserved as 0 in classic NSF)
    80  ..  program data (6502 code + music data), loaded at `load_addr`
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER_SIZE = 0x80
MAGIC = b"NESM\x1a"

# Expansion-chip flag bits at offset 0x7B.
EXP_CHIPS = [
    (0x01, "VRC6"),
    (0x02, "VRC7"),
    (0x04, "FDS"),
    (0x08, "MMC5"),
    (0x10, "Namco163"),
    (0x20, "Sunsoft5B"),
    (0x40, "VT02+"),
]


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("latin-1").strip()


@dataclass
class NSFHeader:
    version: int
    total_songs: int
    starting_song: int
    load_addr: int
    init_addr: int
    play_addr: int
    song_name: str
    artist: str
    copyright: str
    ntsc_speed_us: int
    pal_speed_us: int
    bankswitch: tuple  # 8 ints
    region_flags: int
    chip_flags: int

    @property
    def is_pal(self) -> bool:
        return bool(self.region_flags & 0x01)

    @property
    def is_dual_region(self) -> bool:
        return bool(self.region_flags & 0x02)

    @property
    def uses_bankswitching(self) -> bool:
        return any(self.bankswitch)

    @property
    def expansion_chips(self) -> list:
        return [name for bit, name in EXP_CHIPS if self.chip_flags & bit]

    @property
    def play_rate_hz(self) -> float:
        """Play calls per second, from the region's speed field."""
        us = self.pal_speed_us if self.is_pal else self.ntsc_speed_us
        return 1_000_000.0 / us if us else (50.0 if self.is_pal else 60.0)

    @classmethod
    def parse(cls, data: bytes) -> "NSFHeader":
        if len(data) < HEADER_SIZE:
            raise ValueError(f"file too small to be NSF ({len(data)} bytes)")
        if data[:5] != MAGIC:
            raise ValueError(f"bad NSF magic: {data[:5]!r}")
        (
            version, total, start,
            load_addr, init_addr, play_addr,
        ) = struct.unpack_from("<BBBHHH", data, 0x05)
        song_name = _cstr(data[0x0E:0x2E])
        artist = _cstr(data[0x2E:0x4E])
        copyright_ = _cstr(data[0x4E:0x6E])
        (ntsc_speed,) = struct.unpack_from("<H", data, 0x6E)
        bankswitch = struct.unpack_from("<8B", data, 0x70)
        (pal_speed,) = struct.unpack_from("<H", data, 0x78)
        region_flags = data[0x7A]
        chip_flags = data[0x7B]
        return cls(
            version=version,
            total_songs=total,
            starting_song=start,
            load_addr=load_addr,
            init_addr=init_addr,
            play_addr=play_addr,
            song_name=song_name,
            artist=artist,
            copyright=copyright_,
            ntsc_speed_us=ntsc_speed,
            pal_speed_us=pal_speed,
            bankswitch=bankswitch,
            region_flags=region_flags,
            chip_flags=chip_flags,
        )

    @classmethod
    def from_file(cls, path: str) -> "NSFHeader":
        with open(path, "rb") as fh:
            return cls.parse(fh.read(HEADER_SIZE))
