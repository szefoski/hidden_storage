"""Binary frame protocol shared with firmware/src/rtt_proto.c.

Request:  [opcode:1][sector:4 LE][count:2 LE][payload...][crc16:2 LE]
Response: [opcode|0x80:1][status:1][payload...][crc16:2 LE]
"""
import struct

SECTOR_SIZE = 512
MAX_XFER_SECTORS = 32

OP_INFO = 0x00
OP_READ = 0x01
OP_WRITE = 0x02

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
