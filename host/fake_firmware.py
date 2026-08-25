"""A hardware-free double for firmware/src/main.c + rtt_proto.c + sd_block.c.

Exposes the same rtt_write(channel, data)/rtt_read(channel, n) interface
sdrtt.py's transact()/raw_read_sectors()/raw_write_sectors() call on a real
pylink.JLink, so it can be passed to sdrtt.py's cmd_* functions directly in
place of a real JLink connection.

This is a *second, independent* implementation of the firmware's framing and
dispatch logic (mirroring firmware/src/main.c, not generated from it), so a
mismatch between the two is a real signal, not a tautology. Keep this in
sync by hand whenever the wire protocol or main.c's dispatch logic changes --
there is no other link between the two.
"""
import protocol as proto

RTT_CHANNEL = 1


class FakeFirmware:
    def __init__(self, sector_size=512, sector_count=65536, card_present=True):
        self.sector_size = sector_size
        self.sector_count = sector_count
        self.card_present = card_present
        self.storage = {}  # sector index -> bytes(sector_size)

        self._down_buf = bytearray()  # bytes written by the host, awaiting a full frame
        self._up_buf = bytearray()  # response bytes queued for the host to read

    # --- pylink.JLink-compatible interface -----------------------------

    def rtt_write(self, channel, data):
        assert channel == RTT_CHANNEL
        self._down_buf.extend(data)
        self._process_available_frames()
        return len(data)

    def rtt_read(self, channel, num_bytes):
        assert channel == RTT_CHANNEL
        chunk = bytes(self._up_buf[:num_bytes])
        del self._up_buf[:len(chunk)]
        return chunk

    # --- internals, mirroring rtt_proto.c + main.c ----------------------

    def _process_available_frames(self):
        while True:
            if len(self._down_buf) < 7:
                return
            opcode = self._down_buf[0]
            sector = int.from_bytes(self._down_buf[1:5], "little")
            count = int.from_bytes(self._down_buf[5:7], "little")
            payload_len = count * self.sector_size if opcode == proto.OP_WRITE else 0
            total_len = 7 + payload_len + 2

            if len(self._down_buf) < total_len:
                return  # wait for more bytes, same as rtt_proto_recv_frame's recv_exact loop

            frame = bytes(self._down_buf[:total_len])
            del self._down_buf[:total_len]

            body, crc_bytes = frame[:-2], frame[-2:]
            crc_rx = int.from_bytes(crc_bytes, "little")
            if proto.crc16_ccitt(body) != crc_rx:
                continue  # bad CRC -> silently dropped, no response (matches main.c's `continue`)

            self._handle_request(opcode, sector, count, frame[7:7 + payload_len])

    def _handle_request(self, opcode, sector, count, payload):
        if opcode == proto.OP_INFO:
            status = proto.STATUS_OK if self.card_present else proto.STATUS_ERROR
            resp_payload = self.sector_count.to_bytes(4, "little") + \
                self.sector_size.to_bytes(4, "little")
            self._send_response(opcode, status, resp_payload)
            return

        if opcode == proto.OP_READ:
            if count == 0 or count > proto.MAX_XFER_SECTORS:
                self._send_response(opcode, proto.STATUS_BAD_FRAME, b"")
                return
            data = bytearray(count * self.sector_size)
            status = proto.STATUS_OK
            if not self.card_present:
                status = proto.STATUS_ERROR
            else:
                for i in range(count):
                    sec = self.storage.get(sector + i, b"\x00" * self.sector_size)
                    data[i * self.sector_size:(i + 1) * self.sector_size] = sec
            if status != proto.STATUS_OK:
                data = bytearray(count * self.sector_size)  # zero-filled, matches main.c fix
            self._send_response(opcode, status, bytes(data))
            return

        if opcode == proto.OP_WRITE:
            if count == 0 or count > proto.MAX_XFER_SECTORS:
                self._send_response(opcode, proto.STATUS_BAD_FRAME, b"")
                return
            status = proto.STATUS_OK
            if not self.card_present:
                status = proto.STATUS_ERROR
            else:
                for i in range(count):
                    self.storage[sector + i] = payload[i * self.sector_size:(i + 1) * self.sector_size]
            self._send_response(opcode, status, b"")
            return

        self._send_response(opcode, proto.STATUS_BAD_FRAME, b"")

    def _send_response(self, opcode, status, payload):
        body = bytes([0x80 | opcode, status]) + payload
        crc = proto.crc16_ccitt(body)
        self._up_buf.extend(body + crc.to_bytes(2, "little"))
