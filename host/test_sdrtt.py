"""Hardware-free round-trip tests for sdrtt.py's command logic, using
fake_firmware.FakeFirmware in place of a real J-Link/board connection.

Requires pylink-square installed (sdrtt.py imports it at module level) --
run via the venv: host/.venv/bin/python host/test_sdrtt.py
"""
import os
import tempfile
from types import SimpleNamespace

import protocol as proto
import sdrtt
from fake_firmware import FakeFirmware


def _tmp_path():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    return path


def test_info_card_present():
    fw = FakeFirmware(sector_size=512, sector_count=1000, card_present=True)
    args = SimpleNamespace()
    assert sdrtt.cmd_info(fw, args) == 0


def test_info_card_absent():
    fw = FakeFirmware(card_present=False)
    args = SimpleNamespace()
    assert sdrtt.cmd_info(fw, args) == 1


def test_write_read_roundtrip_small():
    fw = FakeFirmware(card_present=True)
    src = _tmp_path()
    dst = _tmp_path()
    try:
        with open(src, "wb") as f:
            f.write(b"hello hidden storage")

        assert sdrtt.cmd_write(fw, SimpleNamespace(sector=100, input=src)) == 0
        assert sdrtt.cmd_read(fw, SimpleNamespace(sector=100, output=dst)) == 0

        with open(dst, "rb") as f:
            assert f.read() == b"hello hidden storage"
    finally:
        os.unlink(src)
        os.unlink(dst)


def test_write_read_roundtrip_multi_chunk():
    # 50 sectors' worth of data forces raw_write_sectors/raw_read_sectors to
    # split across more than one RTT request (MAX_XFER_SECTORS == 32).
    fw = FakeFirmware(card_present=True)
    payload = bytes((i % 256 for i in range(50 * proto.SECTOR_SIZE)))
    src = _tmp_path()
    dst = _tmp_path()
    try:
        with open(src, "wb") as f:
            f.write(payload)

        assert sdrtt.cmd_write(fw, SimpleNamespace(sector=0, input=src)) == 0
        assert sdrtt.cmd_read(fw, SimpleNamespace(sector=0, output=dst)) == 0

        with open(dst, "rb") as f:
            assert f.read() == payload
    finally:
        os.unlink(src)
        os.unlink(dst)


def test_read_no_header_fails_cleanly():
    fw = FakeFirmware(card_present=True)  # nothing ever written at sector 5
    dst = _tmp_path()
    try:
        assert sdrtt.cmd_read(fw, SimpleNamespace(sector=5, output=dst)) == 1
    finally:
        os.unlink(dst)


def test_raw_read_explicit_count():
    fw = FakeFirmware(card_present=True)
    fw.storage[10] = b"A" * 512
    fw.storage[11] = b"B" * 512
    dst = _tmp_path()
    try:
        assert sdrtt.cmd_raw_read(fw, SimpleNamespace(sector=10, count=2, output=dst)) == 0
        with open(dst, "rb") as f:
            assert f.read() == b"A" * 512 + b"B" * 512
    finally:
        os.unlink(dst)


def test_dump_restore_roundtrip():
    fw = FakeFirmware(sector_size=512, sector_count=4, card_present=True)
    fw.storage[0] = b"\x01" * 512
    fw.storage[1] = b"\x02" * 512
    fw.storage[2] = b"\x03" * 512
    fw.storage[3] = b"\x04" * 512

    img = _tmp_path()
    try:
        assert sdrtt.cmd_dump(fw, SimpleNamespace(output=img)) == 0

        fw2 = FakeFirmware(sector_size=512, sector_count=4, card_present=True)
        assert sdrtt.cmd_restore(fw2, SimpleNamespace(input=img)) == 0
        assert fw2.storage[0] == b"\x01" * 512
        assert fw2.storage[3] == b"\x04" * 512
    finally:
        os.unlink(img)


def test_progress_callback_fires_for_multi_chunk_transfer():
    fw = FakeFirmware(card_present=True)
    calls = []
    data = b"\x42" * (50 * proto.SECTOR_SIZE)  # 50 sectors, > MAX_XFER_SECTORS (32)

    sdrtt.raw_write_sectors(fw, 0, data, progress=lambda done, total: calls.append((done, total)))

    # Two chunks: 32 sectors then the remaining 18.
    assert calls == [(32, 50), (50, 50)]

    calls.clear()
    sdrtt.raw_read_sectors(fw, 0, 50, progress=lambda done, total: calls.append((done, total)))
    assert calls == [(32, 50), (50, 50)]


def test_progress_for_skips_single_chunk_transfers():
    # A transfer that fits in one RTT request isn't worth a progress bar.
    assert sdrtt._progress_for(proto.MAX_XFER_SECTORS) is None
    assert sdrtt._progress_for(1) is None
    assert sdrtt._progress_for(proto.MAX_XFER_SECTORS + 1) is sdrtt._print_progress


def test_verify_sector_size_mismatch_exits():
    fw = FakeFirmware(sector_size=4096, card_present=True)
    try:
        sdrtt.verify_sector_size(fw)
        assert False, "expected SystemExit on a sector-size mismatch"
    except SystemExit:
        pass


def test_verify_sector_size_match_is_a_no_op():
    fw = FakeFirmware(sector_size=512, card_present=True)
    sdrtt.verify_sector_size(fw)  # must not raise/exit


def test_verify_sector_size_skipped_when_card_absent():
    # INFO itself fails (no card) -> nothing to compare, check is a no-op,
    # letting the calling command hit its own natural error instead.
    fw = FakeFirmware(card_present=False)
    sdrtt.verify_sector_size(fw)  # must not raise/exit


def test_cli_sector_argument_accepts_offset_on_all_three_subcommands():
    parser = sdrtt.build_parser()
    expected_sector = (2 * 1024 * 1024) // proto.SECTOR_SIZE  # 2M

    args = parser.parse_args(["read", "2M", "-o", "/tmp/x"])
    assert args.sector == expected_sector

    args = parser.parse_args(["write", "2M", "-i", "/tmp/x"])
    assert args.sector == expected_sector

    args = parser.parse_args(["raw-read", "2M", "1", "-o", "/tmp/x"])
    assert args.sector == expected_sector


def test_write_read_error_when_card_absent():
    # Regression test for the real bug found on hardware: firmware's READ
    # error response used to omit the payload while the host always expects
    # a response sized to its own requested count, which hung `read` until
    # timeout instead of failing cleanly. This asserts the current (fixed)
    # behavior: a clean, prompt failure, not a hang.
    fw = FakeFirmware(card_present=False)
    src = _tmp_path()
    dst = _tmp_path()
    try:
        with open(src, "wb") as f:
            f.write(b"won't get written, no card")

        assert sdrtt.cmd_write(fw, SimpleNamespace(sector=0, input=src)) == 1
        assert sdrtt.cmd_read(fw, SimpleNamespace(sector=0, output=dst)) == 1
    finally:
        os.unlink(src)
        os.unlink(dst)


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
