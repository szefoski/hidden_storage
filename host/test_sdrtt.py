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
    # Enough sectors to force raw_write_sectors/raw_read_sectors to split
    # across more than one RTT request (derived from MAX_XFER_SECTORS so it
    # stays multi-chunk if the per-request cap is retuned).
    fw = FakeFirmware(card_present=True)
    n_sectors = proto.MAX_XFER_SECTORS + 18
    payload = bytes((i % 256 for i in range(n_sectors * proto.SECTOR_SIZE)))
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
    # Derived from MAX_XFER_SECTORS rather than hardcoded, so this keeps
    # testing "spans exactly two chunks" if the per-request cap is retuned
    # for throughput (it has been: 32 -> 64).
    chunk = proto.MAX_XFER_SECTORS
    total = chunk + 18
    fw = FakeFirmware(card_present=True)
    calls = []
    data = b"\x42" * (total * proto.SECTOR_SIZE)

    sdrtt.raw_write_sectors(fw, 0, data, progress=lambda done, total_: calls.append((done, total_)))

    # Two chunks: a full one, then the remaining 18 sectors.
    assert calls == [(chunk, total), (total, total)]

    calls.clear()
    sdrtt.raw_read_sectors(fw, 0, total, progress=lambda done, total_: calls.append((done, total_)))
    assert calls == [(chunk, total), (total, total)]


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


def test_encrypted_roundtrip():
    # write --encrypt then read --encrypt with the same passphrase round-trips.
    fw = FakeFirmware(card_present=True)
    src, dst = _tmp_path(), _tmp_path()
    try:
        with open(src, "wb") as f:
            f.write(b"top secret payload" * 40)  # spans multiple sectors
        w = SimpleNamespace(sector=200, input=src, encrypt=True, passphrase="hunter2")
        r = SimpleNamespace(sector=200, output=dst, encrypt=True, passphrase="hunter2")
        assert sdrtt.cmd_write(fw, w) == 0
        assert sdrtt.cmd_read(fw, r) == 0
        with open(src, "rb") as f:
            expect = f.read()
        with open(dst, "rb") as f:
            assert f.read() == expect
    finally:
        os.unlink(src)
        os.unlink(dst)


def test_encrypted_wrong_passphrase_fails_cleanly():
    # Reading with the wrong passphrase must not return the plaintext; the
    # header won't validate, so cmd_read reports failure (returns 1).
    fw = FakeFirmware(card_present=True)
    src, dst = _tmp_path(), _tmp_path()
    try:
        with open(src, "wb") as f:
            f.write(b"top secret payload")
        assert sdrtt.cmd_write(
            fw, SimpleNamespace(sector=200, input=src, encrypt=True, passphrase="correct")) == 0
        assert sdrtt.cmd_read(
            fw, SimpleNamespace(sector=200, output=dst, encrypt=True, passphrase="WRONG")) == 1
    finally:
        os.unlink(src)
        os.unlink(dst)


def test_encrypted_write_stores_ciphertext_not_plaintext():
    # After an encrypted write, a RAW read (no ENC flag) of the same sectors
    # must return bytes that differ from the plaintext -- proof the card holds
    # ciphertext, not the original data.
    fw = FakeFirmware(card_present=True)
    src = _tmp_path()
    try:
        plaintext = b"the eagle lands at midnight, bring the briefcase"
        with open(src, "wb") as f:
            f.write(plaintext)
        assert sdrtt.cmd_write(
            fw, SimpleNamespace(sector=200, input=src, encrypt=True, passphrase="pw")) == 0

        # Header sector on the card, read raw:
        sdrtt.unlock(fw, "pw")  # harmless; raw read below doesn't use the key
        raw = sdrtt.raw_read_sectors(fw, 200, 1, encrypt=False)
        assert proto.HEADER_MAGIC not in raw       # header itself is encrypted
        assert plaintext[:8] not in raw            # plaintext not present in the clear
    finally:
        os.unlink(src)


def test_plaintext_write_read_unaffected_by_encryption_support():
    # No encrypt flag -> behaves exactly as before (regression guard for the
    # backward-compatible default).
    fw = FakeFirmware(card_present=True)
    src, dst = _tmp_path(), _tmp_path()
    try:
        with open(src, "wb") as f:
            f.write(b"plain and simple")
        assert sdrtt.cmd_write(fw, SimpleNamespace(sector=10, input=src)) == 0
        assert sdrtt.cmd_read(fw, SimpleNamespace(sector=10, output=dst)) == 0
        with open(dst, "rb") as f:
            assert f.read() == b"plain and simple"
        # And a raw read returns the plaintext, since nothing was encrypted.
        raw = sdrtt.raw_read_sectors(fw, 10, 1, encrypt=False)
        assert b"plain and simple" in raw
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
