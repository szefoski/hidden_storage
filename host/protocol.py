"""Binary frame protocol shared with firmware/src/rtt_proto.c.

Request:  [opcode:1][sector:4 LE][count:2 LE][payload...][crc16:2 LE]
Response: [opcode|0x80:1][status:1][payload...][crc16:2 LE]
"""
import re
import struct

SECTOR_SIZE = 512
MAX_XFER_SECTORS = 64

OP_INFO = 0x00
OP_READ = 0x01
OP_WRITE = 0x02
OP_UNLOCK = 0x03

# Flag bits in the opcode byte. 0x80 marks a response (set by firmware); 0x40
# asks the firmware to encrypt/decrypt a READ/WRITE with the session key set by
# OP_UNLOCK. OP_MASK recovers the base opcode.
FLAG_RESP = 0x80
FLAG_ENC = 0x40
OP_MASK = 0x3F

STATUS_OK = 0x00
STATUS_ERROR = 0x01
STATUS_BAD_FRAME = 0x02

_STATUS_NAMES = {
    STATUS_OK: "OK",
    STATUS_ERROR: "ERROR",
    STATUS_BAD_FRAME: "BAD_FRAME",
}


class ProtocolError(Exception):
    pass


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def build_request(opcode: int, sector: int, count: int, payload: bytes = b"") -> bytes:
    header = struct.pack("<BIH", opcode, sector, count)
    body = header + payload
    crc = crc16_ccitt(body)
    return body + struct.pack("<H", crc)


def parse_response(frame: bytes):
    """Returns (opcode, status, payload). Raises ProtocolError on bad CRC/shape."""
    if len(frame) < 4:
        raise ProtocolError(f"frame too short: {len(frame)} bytes")

    body, crc_bytes = frame[:-2], frame[-2:]
    (crc_rx,) = struct.unpack("<H", crc_bytes)
    if crc16_ccitt(body) != crc_rx:
        raise ProtocolError("CRC mismatch")

    opcode = body[0] & 0x7F
    status = body[1]
    payload = body[2:]
    return opcode, status, payload


def status_name(status: int) -> str:
    return _STATUS_NAMES.get(status, f"0x{status:02x}")


# --- Length header used by sdrtt.py's `write`/`read` commands (host-side only;
# the RTT wire protocol above and the firmware know nothing about this) so
# `read` can recover exactly how many bytes/sectors to fetch without the
# caller needing to remember or pass a count.
#
# Layout: [magic:4]["HSD1"][length:8 LE] = 12 bytes, followed by the file's
# raw bytes, the whole thing sector-padded on disk.
HEADER_MAGIC = b"HSD1"
HEADER_LEN = 12


def build_header(length: int) -> bytes:
    return HEADER_MAGIC + struct.pack("<Q", length)


def parse_header(data: bytes):
    """Returns (ok, length). ok is False if data doesn't start with our magic
    (e.g. sector was never written by this tool, or holds unrelated data)."""
    if len(data) < HEADER_LEN or data[:4] != HEADER_MAGIC:
        return False, 0
    (length,) = struct.unpack("<Q", data[4:12])
    return True, length


# --- Sector-or-offset addressing for sdrtt.py's CLI ---------------------
#
# Lets `write`/`read`/`raw-read` take either a bare sector number (unchanged
# from before) or a human-friendly size-suffixed byte offset like "2M",
# converted to a sector number here before anything else runs.

_SIZE_MULTIPLIERS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KkMmGg])[Bb]?\s*$")


def parse_sector_or_offset(value: str) -> int:
    """Returns a sector number from either a bare integer string (passed
    through unchanged) or a size-suffixed byte offset ("2M", "512K", "1G",
    decimals allowed, case-insensitive, optional trailing "B"). Raises
    ValueError with a clear message on anything else, and on a
    size-suffixed offset that isn't an exact multiple of SECTOR_SIZE
    (refuses to guess/round rather than silently landing on the wrong
    sector)."""
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return int(value)

    m = _SIZE_RE.match(value)
    if not m:
        raise ValueError(
            f"'{value}' is not a valid sector number or size offset "
            "(expected a plain integer, or e.g. '2M', '512K', '1G')"
        )

    number_str, unit = m.group(1), m.group(2).lower()
    byte_offset = round(float(number_str) * _SIZE_MULTIPLIERS[unit])

    if byte_offset % SECTOR_SIZE != 0:
        lo = (byte_offset // SECTOR_SIZE) * SECTOR_SIZE
        hi = lo + SECTOR_SIZE
        raise ValueError(
            f"'{value}' ({byte_offset} bytes) is not a multiple of the "
            f"{SECTOR_SIZE}-byte sector size -- nearest valid offsets are "
            f"{lo} bytes (sector {lo // SECTOR_SIZE}) or "
            f"{hi} bytes (sector {hi // SECTOR_SIZE})"
        )

    return byte_offset // SECTOR_SIZE
