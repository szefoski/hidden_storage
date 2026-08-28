#!/usr/bin/env python3
"""CLI to talk to the hidden-SD firmware over J-Link RTT.

Speaks the same binary frame protocol as firmware/src/rtt_proto.c on RTT
buffer index 1 (buffer 0 is left for the normal Zephyr log/console).
"""
import argparse
import getpass
import os
import sys
import time
from pathlib import Path

import pylink

import protocol as proto

# Env var checked for the encryption passphrase before falling back to an
# interactive prompt, so encrypted transfers can be scripted.
PASSPHRASE_ENV = "SDRTT_PASSPHRASE"

RTT_CHANNEL = 1
DEFAULT_DEVICE = "NRF54L15_M33"
# SWD clock for the debug probe, in kHz. RTT throughput scales almost linearly
# with this; 2000 is the J-Link default and was the transfer bottleneck.
SWD_SPEED_KHZ = 16000


def connect(device: str) -> pylink.JLink:
    jlink = pylink.JLink()
    try:
        jlink.open()
    except TypeError as e:
        # pylink raises TypeError (not its own JLinkException) when it can't
        # find/load the SEGGER J-Link DLL/shared library at all -- give a
        # clearer pointer than the raw pylink error in that case.
        sys.exit(
            "Could not load the SEGGER J-Link library. Install the J-Link "
            "Software and Documentation Pack from segger.com/downloads/jlink "
            f"and make sure it's on PATH.\n({e})"
        )
    try:
        jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
        jlink.connect(device)
        # RTT moves data by having the debug probe read target RAM over SWD,
        # so the SWD clock -- not the SPI bus and not this protocol -- is the
        # hard ceiling on throughput. SWD needs roughly 50 clock cycles per
        # 32-bit word, so 2MHz works out to about 160KB/s of theoretical
        # bandwidth, which brackets the ~98KB/s measured here.
        #
        # The nRF54L15-DK's on-board J-Link is FIXED at 2MHz: requesting
        # anything higher is accepted silently, still reports 2000kHz, and
        # measurably changes nothing (4MiB took 42.74s either way). This call
        # is kept because a standalone J-Link probe does honour it, and that
        # is the only way to go faster than ~98KB/s here.
        try:
            jlink.set_speed(SWD_SPEED_KHZ)
        except pylink.errors.JLinkException:
            pass  # keep the probe's default rather than failing the connection
        jlink.rtt_start()
    except pylink.errors.JLinkException as e:
        sys.exit(
            f"J-Link connect failed for device '{device}': {e}\n"
            "Check the board is plugged in and not in use by another tool "
            "(JLinkExe, a debugger, etc)."
        )

    # Wait for the firmware's RTT control block to expose our channel.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            num_up = jlink.rtt_get_num_up_buffers()
            num_down = jlink.rtt_get_num_down_buffers()
            if num_up > RTT_CHANNEL and num_down > RTT_CHANNEL:
                drain_channel(jlink)
                return jlink
        except pylink.errors.JLinkRTTException:
            pass
        time.sleep(0.1)
    raise RuntimeError("Timed out waiting for RTT channel %d" % RTT_CHANNEL)


def drain_channel(jlink: pylink.JLink, quiet: float = 0.15, limit: float = 2.0) -> int:
    """Discard anything already sitting in the firmware's RTT up-buffer.

    Each sdrtt.py run is a separate process, but the firmware's up-buffer
    persists across them. Without this, bytes left over from an earlier run
    (or a response written after that run exited) are read as the beginning
    of this run's response, so every reply is one command stale -- and since
    transact() only checks the opcode, a stale READ response validates fine
    and silently returns another sector's data. Drain until the channel has
    stayed quiet for `quiet` seconds.
    """
    discarded = 0
    deadline = time.time() + limit
    last_data = time.time()
    while time.time() < deadline and (time.time() - last_data) < quiet:
        try:
            chunk = jlink.rtt_read(RTT_CHANNEL, 4096)
        except pylink.errors.JLinkRTTException:
            break
        if chunk:
            discarded += len(chunk)
            last_data = time.time()
        else:
            time.sleep(0.01)
    return discarded


def rtt_write_all(jlink: pylink.JLink, data: bytes) -> None:
    buf = list(data)
    while buf:
        n = jlink.rtt_write(RTT_CHANNEL, buf)
        if n == 0:
            time.sleep(0.0002)
            continue
        buf = buf[n:]


def rtt_read_exact(jlink: pylink.JLink, length: int, timeout: float = 5.0) -> bytes:
    out = bytearray()
    deadline = time.time() + timeout
    while len(out) < length:
        chunk = jlink.rtt_read(RTT_CHANNEL, length - len(out))
        if chunk:
            out.extend(chunk)
            continue
        if time.time() > deadline:
            raise TimeoutError(f"RTT read timed out ({len(out)}/{length} bytes)")
        time.sleep(0.0002)
    return bytes(out)


def transact(jlink, opcode, sector, count, payload=b"", expected_payload_len=0,
             read_timeout=5.0):
    req = proto.build_request(opcode, sector, count, payload)
    rtt_write_all(jlink, req)

    # response header: opcode|0x80 (1) + status (1)
    header = rtt_read_exact(jlink, 2, timeout=read_timeout)
    body_rest = rtt_read_exact(jlink, expected_payload_len + 2, timeout=read_timeout)
    frame = header + body_rest

    resp_opcode, status, payload_out = proto.parse_response(frame)
    # The firmware echoes the request opcode byte (flags included) with the
    # response marker added; parse_response strips the marker, so this compares
    # equal to what we sent, ENC flag and all.
    if resp_opcode != opcode:
        raise proto.ProtocolError(f"opcode mismatch: sent {opcode}, got {resp_opcode}")
    return status, payload_out


def unlock(jlink, passphrase: str) -> None:
    """Sends the passphrase to the firmware, which derives the session AES key
    on-device (PBKDF2 on CRACEN). Raises RuntimeError if the firmware rejects
    it. The passphrase length travels in the frame's `count` field, the bytes
    in the payload."""
    pw = passphrase.encode("utf-8")
    if not pw:
        raise RuntimeError("empty passphrase")
    if len(pw) > 128:
        raise RuntimeError("passphrase too long (max 128 bytes)")
    # On-device PBKDF2 takes a couple of seconds; allow generous headroom so a
    # slower run never trips the default per-transaction timeout.
    status, _ = transact(jlink, proto.OP_UNLOCK, 0, len(pw), payload=pw, read_timeout=30.0)
    if status != proto.STATUS_OK:
        raise RuntimeError(f"unlock failed: {proto.status_name(status)}")


def resolve_passphrase(args) -> str:
    """Passphrase from --passphrase, else $SDRTT_PASSPHRASE, else a prompt."""
    if getattr(args, "passphrase", None):
        return args.passphrase
    env = os.environ.get(PASSPHRASE_ENV)
    if env:
        return env
    return getpass.getpass("Passphrase: ")


def cmd_info(jlink, args):
    status, payload = transact(jlink, proto.OP_INFO, 0, 0, expected_payload_len=8)
    if status != proto.STATUS_OK:
        print(f"INFO failed: {proto.status_name(status)}", file=sys.stderr)
        return 1
    sector_count, sector_size = int.from_bytes(payload[0:4], "little"), int.from_bytes(
        payload[4:8], "little"
    )
    print(f"sector_size={sector_size} sector_count={sector_count} "
          f"capacity={sector_count * sector_size / (1024*1024):.1f} MiB")
    return 0


def verify_sector_size(jlink) -> None:
    """Fails loudly if the card's real sector size (queried via INFO)
    doesn't match the SECTOR_SIZE constant all of this tool's chunking math
    assumes -- rather than silently doing the wrong sector-boundary math.
    If INFO itself fails (no card yet), does nothing; the calling command
    will hit its own natural error.
    """
    status, payload = transact(jlink, proto.OP_INFO, 0, 0, expected_payload_len=8)
    if status != proto.STATUS_OK:
        return
    sector_size = int.from_bytes(payload[4:8], "little")
    if sector_size != proto.SECTOR_SIZE:
        sys.exit(
            f"Card reports sector_size={sector_size}, but this tool assumes "
            f"{proto.SECTOR_SIZE} throughout its chunking logic -- refusing "
            "to proceed rather than risk corrupting data with the wrong "
            "sector-boundary math."
        )


def _print_progress(done: int, total: int) -> None:
    """Renders an in-place text progress bar to stderr. Call with done==total
    last to print the final 100% state and its trailing newline."""
    width = 30
    frac = done / total if total else 1.0
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    end = "\n" if done >= total else ""
    print(f"\r[{bar}] {frac * 100:3.0f}% ({done}/{total} sectors)", end=end,
          file=sys.stderr, flush=True)


def raw_read_sectors(jlink, sector: int, count: int, progress=None, encrypt=False) -> bytes:
    """Reads `count` sectors starting at `sector`, chunked under the wire
    protocol's per-request cap. Raises RuntimeError on a non-OK status.
    If given, `progress(sectors_done, sectors_total)` is called after each
    chunk. With encrypt=True, sets the ENC flag so the firmware decrypts each
    sector with the session key (unlock() must have run first)."""
    opcode = proto.OP_READ | (proto.FLAG_ENC if encrypt else 0)
    out = bytearray()
    remaining = count
    done = 0
    while remaining > 0:
        chunk = min(remaining, proto.MAX_XFER_SECTORS)
        status, payload = transact(
            jlink, opcode, sector, chunk,
            expected_payload_len=chunk * proto.SECTOR_SIZE,
        )
        if status != proto.STATUS_OK:
            raise RuntimeError(f"READ failed at sector {sector}: {proto.status_name(status)}")
        out.extend(payload)
        sector += chunk
        remaining -= chunk
        done += chunk
        if progress:
            progress(done, count)
    return bytes(out)


def raw_write_sectors(jlink, sector: int, data: bytes, progress=None, encrypt=False) -> None:
    """Writes `data` (must already be a multiple of SECTOR_SIZE) starting at
    `sector`, chunked under the wire protocol's per-request cap.
    If given, `progress(sectors_done, sectors_total)` is called after each
    chunk. With encrypt=True, sets the ENC flag so the firmware encrypts each
    sector with the session key (unlock() must have run first)."""
    opcode = proto.OP_WRITE | (proto.FLAG_ENC if encrypt else 0)
    total_sectors = len(data) // proto.SECTOR_SIZE
    off = 0
    remaining = total_sectors
    done = 0
    while remaining > 0:
        chunk = min(remaining, proto.MAX_XFER_SECTORS)
        payload = data[off:off + chunk * proto.SECTOR_SIZE]
        status, _ = transact(jlink, opcode, sector, chunk, payload=payload)
        if status != proto.STATUS_OK:
            raise RuntimeError(f"WRITE failed at sector {sector}: {proto.status_name(status)}")
        sector += chunk
        off += chunk * proto.SECTOR_SIZE
        remaining -= chunk
        done += chunk
        if progress:
            progress(done, total_sectors)


def _progress_for(total_sectors: int):
    """Returns a progress callback for cmd_* to pass through, or None for
    transfers small enough to finish in a single RTT request (not worth the
    noise of a bar for those)."""
    if total_sectors <= proto.MAX_XFER_SECTORS:
        return None
    return _print_progress


def cmd_write(jlink, args):
    """Writes a file starting at `sector`, prefixed with a length header so
    `read` can later recover exactly how many bytes/sectors to fetch without
    being told. With --encrypt, the firmware encrypts every sector (header
    included) on the card."""
    encrypt = getattr(args, "encrypt", False)
    if encrypt:
        try:
            unlock(jlink, resolve_passphrase(args))
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 1

    file_data = Path(args.input).read_bytes()

    blob = proto.build_header(len(file_data)) + file_data
    total_sectors = (len(blob) + proto.SECTOR_SIZE - 1) // proto.SECTOR_SIZE
    blob = blob.ljust(total_sectors * proto.SECTOR_SIZE, b"\x00")

    try:
        raw_write_sectors(jlink, args.sector, blob,
                          progress=_progress_for(total_sectors), encrypt=encrypt)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1

    print(f"Wrote {len(file_data)} bytes ({total_sectors} sectors) "
          f"{'encrypted ' if encrypt else ''}starting at sector {args.sector} <- {args.input}")
    return 0


def cmd_read(jlink, args):
    """Reads back a file previously written with `write`. Discovers the
    length from the header `write` embedded, so no count is needed. With
    --encrypt, the firmware decrypts every sector; the passphrase must match
    the one used to write, or the header won't validate."""
    encrypt = getattr(args, "encrypt", False)
    if encrypt:
        try:
            unlock(jlink, resolve_passphrase(args))
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 1

    try:
        header_sector = raw_read_sectors(jlink, args.sector, 1, encrypt=encrypt)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1

    ok, length = proto.parse_header(header_sector)
    if not ok:
        hint = ("wrong passphrase, or " if encrypt else "")
        print(f"No valid header at sector {args.sector} -- {hint}was this written with "
              f"`sdrtt.py write`{' --encrypt' if encrypt else ''}? "
              f"For sectors written another way, use `raw-read`.",
              file=sys.stderr)
        return 1

    total_sectors = (proto.HEADER_LEN + length + proto.SECTOR_SIZE - 1) // proto.SECTOR_SIZE
    try:
        blob = raw_read_sectors(jlink, args.sector, total_sectors,
                                progress=_progress_for(total_sectors), encrypt=encrypt)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1

    file_data = blob[proto.HEADER_LEN:proto.HEADER_LEN + length]
    Path(args.output).write_bytes(file_data)

    print(f"Read {length} bytes ({total_sectors} sectors) "
          f"{'encrypted ' if encrypt else ''}from sector {args.sector} -> {args.output}")
    return 0


def cmd_raw_read(jlink, args):
    """Explicit-count raw sector read, no header involved -- for data not
    written by `sdrtt.py write` (e.g. inspecting arbitrary card contents)."""
    try:
        data = raw_read_sectors(jlink, args.sector, args.count, progress=_progress_for(args.count))
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    Path(args.output).write_bytes(data)
    print(f"Read {args.count} raw sectors starting at {args.sector} -> {args.output}")
    return 0


def cmd_dump(jlink, args):
    status, payload = transact(jlink, proto.OP_INFO, 0, 0, expected_payload_len=8)
    if status != proto.STATUS_OK:
        print(f"INFO failed: {proto.status_name(status)}", file=sys.stderr)
        return 1
    sector_count = int.from_bytes(payload[0:4], "little")
    try:
        data = raw_read_sectors(jlink, 0, sector_count, progress=_progress_for(sector_count))
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    Path(args.output).write_bytes(data)
    print(f"Dumped {sector_count} sectors -> {args.output}")
    return 0


def cmd_restore(jlink, args):
    data = Path(args.input).read_bytes()
    if len(data) % proto.SECTOR_SIZE != 0:
        print(f"Image size {len(data)} is not a multiple of {proto.SECTOR_SIZE} "
              f"bytes -- refusing to guess padding for a whole-card restore.",
              file=sys.stderr)
        return 1
    restore_sectors = len(data) // proto.SECTOR_SIZE
    try:
        raw_write_sectors(jlink, 0, data, progress=_progress_for(restore_sectors))
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Restored {restore_sectors} sectors <- {args.input}")
    return 0


def _sector_type(value):
    try:
        return proto.parse_sector_or_offset(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


_SECTOR_HELP = "sector number, or a size offset like '2M'/'512K'/'1G'"


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device", default=DEFAULT_DEVICE,
                    help=f"J-Link device name (default: {DEFAULT_DEVICE})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="print sector size and sector count")

    def add_encrypt_opts(sp):
        sp.add_argument("-e", "--encrypt", action="store_true",
                        help="encrypt/decrypt on the device (CRACEN). Passphrase from "
                             f"--passphrase, ${PASSPHRASE_ENV}, or an interactive prompt")
        sp.add_argument("--passphrase",
                        help=f"encryption passphrase (else ${PASSPHRASE_ENV} or a prompt)")

    p_read = sub.add_parser(
        "read", help="read back a file previously written with `write` (size is auto-detected)")
    p_read.add_argument("sector", type=_sector_type, help=_SECTOR_HELP)
    p_read.add_argument("-o", "--output", required=True)
    add_encrypt_opts(p_read)

    p_write = sub.add_parser(
        "write", help="write a file starting at sector (embeds a length header for `read`)")
    p_write.add_argument("sector", type=_sector_type, help=_SECTOR_HELP)
    p_write.add_argument("-i", "--input", required=True)
    add_encrypt_opts(p_write)

    p_raw_read = sub.add_parser(
        "raw-read", help="read an explicit sector count, no header (for data not written by `write`)")
    p_raw_read.add_argument("sector", type=_sector_type, help=_SECTOR_HELP)
    p_raw_read.add_argument("count", type=int)
    p_raw_read.add_argument("-o", "--output", required=True)

    p_dump = sub.add_parser("dump", help="read the whole card to a file")
    p_dump.add_argument("-o", "--output", required=True)

    p_restore = sub.add_parser("restore", help="write a whole-card image starting at sector 0")
    p_restore.add_argument("-i", "--input", required=True)

    return p


def main():
    args = build_parser().parse_args()
    if getattr(args, "passphrase", None) and not getattr(args, "encrypt", False):
        print("note: --passphrase given without --encrypt; ignoring it and "
              "transferring in plaintext.", file=sys.stderr)
    jlink = connect(args.device)
    try:
        if args.cmd == "info":
            return cmd_info(jlink, args)
        verify_sector_size(jlink)
        if args.cmd == "read":
            return cmd_read(jlink, args)
        if args.cmd == "write":
            return cmd_write(jlink, args)
        if args.cmd == "raw-read":
            return cmd_raw_read(jlink, args)
        if args.cmd == "dump":
            return cmd_dump(jlink, args)
        if args.cmd == "restore":
            return cmd_restore(jlink, args)
    finally:
        jlink.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
