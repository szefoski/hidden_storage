"""Hardware-free round-trip tests for the RTT frame protocol."""
import struct

import protocol as proto


def test_crc16_known_vector():
    # CRC16-CCITT (0xFFFF init, poly 0x1021) of ASCII "123456789" is 0x29B1.
    assert proto.crc16_ccitt(b"123456789") == 0x29B1


def test_request_round_trip_no_payload():
    req = proto.build_request(proto.OP_INFO, sector=0, count=0)
    body, crc_bytes = req[:-2], req[-2:]
    (crc_rx,) = struct.unpack("<H", crc_bytes)
    assert proto.crc16_ccitt(body) == crc_rx
    assert body[0] == proto.OP_INFO


def test_request_round_trip_with_payload():
    payload = bytes(range(16)) * 32  # 512 bytes, one fake sector
    req = proto.build_request(proto.OP_WRITE, sector=42, count=1, payload=payload)
    assert len(req) == 1 + 4 + 2 + len(payload) + 2


def test_parse_response_ok():
    payload = b"\x01" * 8
    body = bytes([proto.OP_INFO | 0x80, proto.STATUS_OK]) + payload
    crc = proto.crc16_ccitt(body)
    frame = body + struct.pack("<H", crc)

    opcode, status, out_payload = proto.parse_response(frame)
    assert opcode == proto.OP_INFO
    assert status == proto.STATUS_OK
    assert out_payload == payload


def test_parse_response_bad_crc():
    body = bytes([proto.OP_READ | 0x80, proto.STATUS_OK])
    frame = body + b"\x00\x00"  # wrong CRC
    try:
        proto.parse_response(frame)
        assert False, "expected ProtocolError"
    except proto.ProtocolError:
        pass


def test_header_round_trip():
    header = proto.build_header(12345)
    assert len(header) == proto.HEADER_LEN
    ok, length = proto.parse_header(header)
    assert ok
    assert length == 12345


def test_header_round_trip_with_trailing_data():
    header = proto.build_header(3)
    blob = header + b"abc" + b"\x00" * 100  # simulates sector padding
    ok, length = proto.parse_header(blob)
    assert ok
    assert length == 3
    assert blob[proto.HEADER_LEN:proto.HEADER_LEN + length] == b"abc"


def test_parse_header_rejects_garbage():
    ok, _ = proto.parse_header(b"\x00" * 20)
    assert not ok


def test_parse_header_rejects_short_input():
    ok, _ = proto.parse_header(b"HSD1\x01\x02")
    assert not ok


def test_parse_sector_or_offset_plain_int():
    assert proto.parse_sector_or_offset("1000") == 1000
    assert proto.parse_sector_or_offset("0") == 0


def test_parse_sector_or_offset_size_suffixes():
    assert proto.parse_sector_or_offset("512K") == (512 * 1024) // proto.SECTOR_SIZE
    assert proto.parse_sector_or_offset("2M") == (2 * 1024 * 1024) // proto.SECTOR_SIZE
    assert proto.parse_sector_or_offset("1G") == (1024 * 1024 * 1024) // proto.SECTOR_SIZE


def test_parse_sector_or_offset_case_and_b_suffix():
    assert proto.parse_sector_or_offset("2m") == proto.parse_sector_or_offset("2M")
    assert proto.parse_sector_or_offset("2MB") == proto.parse_sector_or_offset("2M")
    assert proto.parse_sector_or_offset("2mb") == proto.parse_sector_or_offset("2M")


def test_parse_sector_or_offset_decimal():
    # 1.5 MiB = 1572864 bytes = 3072 sectors of 512 bytes, exact.
    assert proto.parse_sector_or_offset("1.5M") == 1572864 // proto.SECTOR_SIZE


def test_parse_sector_or_offset_rejects_unaligned():
    # 0.001M = 1048.576 bytes -- not a multiple of the 512-byte sector size.
    try:
        proto.parse_sector_or_offset("0.001M")
        assert False, "expected ValueError for a non-sector-aligned offset"
    except ValueError as e:
        assert "not a multiple" in str(e)


def test_parse_sector_or_offset_rejects_garbage():
    try:
        proto.parse_sector_or_offset("not-a-number")
        assert False, "expected ValueError"
    except ValueError:
        pass


if __name__ == "__main__":
    import sys

    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
