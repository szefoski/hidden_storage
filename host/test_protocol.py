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
