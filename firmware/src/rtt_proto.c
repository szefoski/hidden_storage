#include "rtt_proto.h"
#include "sd_block.h"

#include <string.h>
#include <zephyr/kernel.h>
#include <SEGGER_RTT.h>

#define HEADER_LEN 7 /* opcode(1) + sector(4) + count(2) */
#define CRC_LEN 2

static uint16_t rd_le16(const uint8_t *p)
{
	return (uint16_t)(p[0] | ((uint16_t)p[1] << 8));
}

uint16_t rtt_proto_crc16(const uint8_t *data, size_t len)
{
	uint16_t crc = 0xFFFF;

	for (size_t i = 0; i < len; i++) {
		crc ^= (uint16_t)data[i] << 8;
		for (int b = 0; b < 8; b++) {
			crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
		}
	}
	return crc;
}

void rtt_proto_init(void)
{
	static uint8_t up_buf[SD_BLOCK_MAX_XFER_SECTORS * SD_SECTOR_SIZE + 64];
	static uint8_t down_buf[SD_BLOCK_MAX_XFER_SECTORS * SD_SECTOR_SIZE + 64];

	SEGGER_RTT_ConfigUpBuffer(RTT_PROTO_CHANNEL, "sd_up", up_buf, sizeof(up_buf),
				   SEGGER_RTT_MODE_NO_BLOCK_SKIP);
	SEGGER_RTT_ConfigDownBuffer(RTT_PROTO_CHANNEL, "sd_down", down_buf, sizeof(down_buf),
				     SEGGER_RTT_MODE_NO_BLOCK_SKIP);
}

static void recv_exact(uint8_t *dst, size_t len)
{
	size_t got = 0;

	while (got < len) {
		unsigned n = SEGGER_RTT_Read(RTT_PROTO_CHANNEL, dst + got, len - got);
		if (n == 0) {
			k_sleep(K_USEC(100));
			continue;
		}
		got += n;
	}
}

size_t rtt_proto_recv_frame(uint8_t *buf, size_t max_len)
{
	if (max_len < HEADER_LEN + CRC_LEN) {
		return 0;
	}

	recv_exact(buf, HEADER_LEN);

	uint8_t opcode = buf[0];
	uint16_t count = rd_le16(&buf[5]);
	size_t payload_len = 0;

	if (opcode == RTT_PROTO_OP_WRITE) {
		payload_len = (size_t)count * SD_SECTOR_SIZE;
	}

	size_t total_len = HEADER_LEN + payload_len + CRC_LEN;

	if (total_len > max_len) {
		/* Drain and discard -- malformed/oversized request. */
		uint8_t scratch[64];
		size_t remaining = total_len - HEADER_LEN;

		while (remaining > 0) {
			size_t chunk = remaining < sizeof(scratch) ? remaining : sizeof(scratch);

			recv_exact(scratch, chunk);
			remaining -= chunk;
		}
		return 0;
	}

	recv_exact(buf + HEADER_LEN, payload_len + CRC_LEN);

	uint16_t crc_rx = rd_le16(&buf[total_len - CRC_LEN]);
	uint16_t crc_calc = rtt_proto_crc16(buf, total_len - CRC_LEN);

	if (crc_rx != crc_calc) {
		return 0;
	}

	return total_len;
}

void rtt_proto_send_frame(const uint8_t *buf, size_t len)
{
	size_t sent = 0;

	while (sent < len) {
		unsigned n = SEGGER_RTT_Write(RTT_PROTO_CHANNEL, buf + sent, len - sent);
		if (n == 0) {
			k_sleep(K_USEC(100));
			continue;
		}
		sent += n;
	}
}
