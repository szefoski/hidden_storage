#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <string.h>

#include "rtt_proto.h"
#include "sd_block.h"

LOG_MODULE_REGISTER(main, LOG_LEVEL_INF);

#define REQ_BUF_LEN (7 + SD_BLOCK_MAX_XFER_SECTORS * SD_SECTOR_SIZE + 2)
#define RSP_BUF_LEN (2 + SD_BLOCK_MAX_XFER_SECTORS * SD_SECTOR_SIZE + 2)

static uint8_t req_buf[REQ_BUF_LEN];
static uint8_t rsp_buf[RSP_BUF_LEN];

static uint32_t rd_le32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
	       ((uint32_t)p[3] << 24);
}

static uint16_t rd_le16(const uint8_t *p)
{
	return (uint16_t)(p[0] | ((uint16_t)p[1] << 8));
}

static void wr_le16(uint8_t *p, uint16_t v)
{
	p[0] = (uint8_t)(v & 0xFF);
	p[1] = (uint8_t)(v >> 8);
}

static void wr_le32(uint8_t *p, uint32_t v)
{
	p[0] = (uint8_t)(v & 0xFF);
	p[1] = (uint8_t)((v >> 8) & 0xFF);
	p[2] = (uint8_t)((v >> 16) & 0xFF);
	p[3] = (uint8_t)((v >> 24) & 0xFF);
}

/* Appends status+payload to rsp_buf, appends CRC16, sends it. */
static void send_response(uint8_t opcode, uint8_t status, const uint8_t *payload,
			   size_t payload_len)
{
	size_t len = 0;

	rsp_buf[len++] = (uint8_t)(0x80 | opcode);
	rsp_buf[len++] = status;
	if (payload_len > 0) {
		memcpy(&rsp_buf[len], payload, payload_len);
		len += payload_len;
	}
	uint16_t crc = rtt_proto_crc16(rsp_buf, len);

	wr_le16(&rsp_buf[len], crc);
	len += 2;

	rtt_proto_send_frame(rsp_buf, len);
}

int main(void)
{
	int rc = sd_block_init();

	if (rc != 0) {
		LOG_ERR("SD init failed (%d) -- RTT link still up, INFO/READ/WRITE will report errors", rc);
	}

	rtt_proto_init();
	LOG_INF("Hidden SD relay ready on RTT channel %d", RTT_PROTO_CHANNEL);

	while (1) {
		size_t frame_len = rtt_proto_recv_frame(req_buf, sizeof(req_buf));

		if (frame_len == 0) {
			continue; /* framing/CRC error, or nothing to do yet */
		}

		uint8_t opcode = req_buf[0];
		uint32_t sector = rd_le32(&req_buf[1]);
		uint16_t count = rd_le16(&req_buf[5]);

		switch (opcode) {
		case RTT_PROTO_OP_INFO: {
			uint32_t sector_count = 0, sector_size = 0;
			uint8_t payload[8];
			uint8_t status = RTT_PROTO_STATUS_OK;

			if (rc != 0 || sd_block_get_info(&sector_count, &sector_size) != 0) {
				status = RTT_PROTO_STATUS_ERROR;
			}
			wr_le32(&payload[0], sector_count);
			wr_le32(&payload[4], sector_size);
			send_response(opcode, status, payload, sizeof(payload));
			break;
		}
		case RTT_PROTO_OP_READ: {
			if (count == 0 || count > SD_BLOCK_MAX_XFER_SECTORS) {
				send_response(opcode, RTT_PROTO_STATUS_BAD_FRAME, NULL, 0);
				break;
			}
			static uint8_t data[SD_BLOCK_MAX_XFER_SECTORS * SD_SECTOR_SIZE];
			uint8_t status = RTT_PROTO_STATUS_OK;

			if (rc != 0 || sd_block_read(sector, count, data) != 0) {
				status = RTT_PROTO_STATUS_ERROR;
				send_response(opcode, status, NULL, 0);
			} else {
				send_response(opcode, status, data, (size_t)count * SD_SECTOR_SIZE);
			}
			break;
		}
		case RTT_PROTO_OP_WRITE: {
			if (count == 0 || count > SD_BLOCK_MAX_XFER_SECTORS) {
				send_response(opcode, RTT_PROTO_STATUS_BAD_FRAME, NULL, 0);
				break;
			}
			uint8_t status = RTT_PROTO_STATUS_OK;

			if (rc != 0 || sd_block_write(sector, count, &req_buf[7]) != 0) {
				status = RTT_PROTO_STATUS_ERROR;
			}
			send_response(opcode, status, NULL, 0);
			break;
		}
		default:
			send_response(opcode, RTT_PROTO_STATUS_BAD_FRAME, NULL, 0);
			break;
		}
	}

	return 0;
}
