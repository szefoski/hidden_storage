#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/drivers/gpio.h>
#include <string.h>

#include "rtt_proto.h"
#include "sd_block.h"
#include "crypto.h"

LOG_MODULE_REGISTER(main, LOG_LEVEL_INF);

/* Bring-up diagnostic, off by default. Set to 1 to run a raw SPI loopback
 * test at boot that is independent of the SD card and the SD driver: it
 * transmits a known pattern and checks it comes back byte-for-byte.
 *
 * To use it, set this to 1 and bridge MOSI (P1.08) directly to MISO (P1.11)
 * with a single jumper at the DK's P1 header, with the SD card and all its
 * wiring disconnected. A PASS proves the SPI22 peripheral and those pins are
 * electrically sound, which cleanly separates "the bus is broken" from "the
 * card or its wiring is broken" -- this is what identified the debugger-UART
 * pin conflict documented in the board overlay. With the card connected
 * instead of the jumper this reports FAIL with all-0xff, which is expected
 * (the card holds MISO high and never echoes), so it is only meaningful in
 * the jumper configuration above. */
#define SD_SPI_LOOPBACK_TEST 0

#if DT_NODE_HAS_STATUS_OKAY(DT_NODELABEL(sdhc0))
/* Send the SD spec's mandatory power-up clocks with chip select held
 * INACTIVE, before the SD stack initialises the card.
 *
 * Zephyr's sdhc_spi_init_card() already tries to do this: it sets
 * SPI_CS_ACTIVE_HIGH on the SPI config, sends 10 bytes of 0xff, then clears
 * the flag again -- the comment there even says "the initial 74 clocks must
 * be sent while CS is high". But that flag is never honoured on this
 * platform. The generic CS handling in drivers/spi/spi_context.h does only:
 *
 *     if (on)  gpio_pin_set_dt(&ctx->config->cs.gpio, 1);
 *     else     gpio_pin_set_dt(&ctx->config->cs.gpio, 0);
 *
 * with no reference to SPI_CS_ACTIVE_HIGH, and neither spi_context nor
 * spi_nrfx_spim.c implements it (only a few vendor drivers do). So on nRF
 * those priming clocks are actually clocked out with CS asserted LOW, which
 * violates the SD initialisation sequence. Cards that enforce it never enter
 * SPI mode cleanly and then answer with malformed CID/CSD registers -- the
 * CSD comes back failing its own CRC7, which decodes to a bogus capacity and
 * a bogus addressing mode. See zephyrproject-rtos/zephyr issue #29946.
 *
 * The fix is to clock the card ourselves with CS driven inactive by hand,
 * using a spi_config that carries no CS at all so the SPI driver cannot
 * touch the line. Once the card is properly in SPI mode, the SD stack's own
 * (still slightly wrong) init sequence is harmless. */
static int sd_spi_prime_bus(void)
{
	const struct device *bus = DEVICE_DT_GET(DT_BUS(DT_NODELABEL(sdhc0)));
	const struct gpio_dt_spec cs =
		GPIO_DT_SPEC_GET_BY_IDX(DT_BUS(DT_NODELABEL(sdhc0)), cs_gpios, 0);
	/* No .cs member set -> spi_cs_is_gpio() is false -> the driver leaves
	 * the chip select line entirely alone during this transfer. */
	struct spi_config cfg = {
		.frequency = 400000,
		.operation = SPI_WORD_SET(8) | SPI_TRANSFER_MSB | SPI_OP_MODE_MASTER,
		.slave = 0,
	};
	static uint8_t ones[10];
	struct spi_buf tx_b = {.buf = ones, .len = sizeof(ones)};
	struct spi_buf_set tx = {.buffers = &tx_b, .count = 1};
	int ret;

	if (!device_is_ready(bus) || !gpio_is_ready_dt(&cs)) {
		LOG_ERR("PRIME: bus or CS not ready");
		return -ENODEV;
	}

	/* GPIO_OUTPUT_INACTIVE respects the pin's GPIO_ACTIVE_LOW flag, so this
	 * drives CS to its deasserted level (physically high on this board). */
	ret = gpio_pin_configure_dt(&cs, GPIO_OUTPUT_INACTIVE);
	if (ret != 0) {
		LOG_ERR("PRIME: could not drive CS inactive (%d)", ret);
		return ret;
	}

	memset(ones, 0xFF, sizeof(ones));
	/* 10 bytes = 80 clocks, comfortably above the 74 the spec requires. */
	ret = spi_transceive(bus, &cfg, &tx, NULL);
	if (ret != 0) {
		LOG_ERR("PRIME: clock burst failed (%d)", ret);
		return ret;
	}

	k_sleep(K_MSEC(2));
	LOG_INF("PRIME: sent 80 power-up clocks with CS deasserted");
	return 0;
}
#endif

#if SD_SPI_LOOPBACK_TEST && DT_NODE_HAS_STATUS_OKAY(DT_NODELABEL(sdhc0))
static void spi_loopback_test(void)
{
	static const struct spi_dt_spec spec = SPI_DT_SPEC_GET(
		DT_NODELABEL(sdhc0), SPI_WORD_SET(8) | SPI_TRANSFER_MSB | SPI_OP_MODE_MASTER, 0);

	if (!spi_is_ready_dt(&spec)) {
		LOG_ERR("LOOPBACK: SPI bus not ready");
		return;
	}

	/* Non-const, RAM-resident on purpose: SPI30's EasyDMA may not be able
	 * to source TX data directly from flash/XIP -- a const array would be
	 * placed in flash by the compiler and could itself explain corrupted
	 * transfers, independent of anything electrical. */
	static uint8_t tx_pattern[8] = {0xA5, 0x5A, 0x00, 0xFF, 0x3C, 0xC3, 0x81, 0x18};
	uint8_t rx_buf[sizeof(tx_pattern)] = {0};

	struct spi_buf tx_b = {.buf = (void *)tx_pattern, .len = sizeof(tx_pattern)};
	struct spi_buf rx_b = {.buf = rx_buf, .len = sizeof(rx_buf)};
	struct spi_buf_set tx_set = {.buffers = &tx_b, .count = 1};
	struct spi_buf_set rx_set = {.buffers = &rx_b, .count = 1};

	int ret = spi_transceive_dt(&spec, &tx_set, &rx_set);

	if (ret != 0) {
		LOG_ERR("LOOPBACK: spi_transceive failed (%d)", ret);
		return;
	}

	if (memcmp(tx_pattern, rx_buf, sizeof(tx_pattern)) == 0) {
		LOG_INF("LOOPBACK: PASS -- MOSI/MISO/SCK electrically alive, jumper confirmed");
	} else {
		LOG_ERR("LOOPBACK: FAIL -- sent %02x %02x %02x %02x %02x %02x %02x %02x, "
			"got %02x %02x %02x %02x %02x %02x %02x %02x",
			tx_pattern[0], tx_pattern[1], tx_pattern[2], tx_pattern[3],
			tx_pattern[4], tx_pattern[5], tx_pattern[6], tx_pattern[7],
			rx_buf[0], rx_buf[1], rx_buf[2], rx_buf[3],
			rx_buf[4], rx_buf[5], rx_buf[6], rx_buf[7]);
	}
}
#endif

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
/* Payload area of rsp_buf: everything after the opcode and status bytes. */
#define RSP_PAYLOAD (&rsp_buf[2])

/* Wraps a payload the caller has ALREADY written into RSP_PAYLOAD with the
 * frame header and CRC, then sends it.
 *
 * Split out from send_response() so a sector read can land straight in the
 * response buffer. Previously the READ path staged the data in a second
 * full-size buffer and then memcpy'd it here, which cost an extra
 * SD_BLOCK_MAX_XFER_SECTORS*SD_SECTOR_SIZE of RAM (32KB at the current chunk
 * size) plus a 32KB copy on every read. The WRITE path never needed the
 * detour -- it already passes &req_buf[7] straight to sd_block_write(). */
static void send_response_in_place(uint8_t opcode, uint8_t status, size_t payload_len)
{
	size_t len = 2 + payload_len;

	rsp_buf[0] = (uint8_t)(0x80 | opcode);
	rsp_buf[1] = status;

	uint16_t crc = rtt_proto_crc16(rsp_buf, len);

	wr_le16(&rsp_buf[len], crc);
	len += 2;

	rtt_proto_send_frame(rsp_buf, len);
}

static void send_response(uint8_t opcode, uint8_t status, const uint8_t *payload,
			   size_t payload_len)
{
	if (payload_len > 0) {
		memcpy(RSP_PAYLOAD, payload, payload_len);
	}
	send_response_in_place(opcode, status, payload_len);
}

int main(void)
{
	/* Let SPI/GPIO/clock hardware fully settle after boot before the very
	 * first SD-over-SPI attempt. */
	k_sleep(K_MSEC(500));

#if SD_SPI_LOOPBACK_TEST && DT_NODE_HAS_STATUS_OKAY(DT_NODELABEL(sdhc0))
	spi_loopback_test();
#endif

#if DT_NODE_HAS_STATUS_OKAY(DT_NODELABEL(sdhc0))
	/* Must run before the SD stack touches the card -- see the comment on
	 * sd_spi_prime_bus() for why Zephyr's own priming does not work here. */
	(void)sd_spi_prime_bus();
#endif

	/* Retry the whole init sequence a few times before giving up --
	 * cheap robustness against a transient failure on any single
	 * attempt. */
	int rc = -1;

	for (int attempt = 1; attempt <= 5 && rc != 0; attempt++) {
		rc = sd_block_init();
		if (rc != 0) {
			LOG_WRN("SD init attempt %d/5 failed (%d), retrying", attempt, rc);
			k_sleep(K_MSEC(200));
		}
	}

	if (rc != 0) {
		LOG_ERR("SD init failed (%d) -- RTT link still up, INFO/READ/WRITE will report errors", rc);
	}

	if (crypto_init() != 0) {
		LOG_ERR("crypto init failed -- encrypted READ/WRITE will report errors");
	}

	rtt_proto_init();
	LOG_INF("Hidden SD relay ready on RTT channel %d", RTT_PROTO_CHANNEL);

	while (1) {
		size_t frame_len = rtt_proto_recv_frame(req_buf, sizeof(req_buf));

		if (frame_len == 0) {
			continue; /* framing/CRC error, or nothing to do yet */
		}

		/* The received opcode byte carries the ENC flag in its high nibble;
		 * keep it intact for the response (so the host's opcode echo check
		 * passes) but switch on the masked base opcode. */
		uint8_t opcode = req_buf[0];
		uint8_t op = opcode & RTT_PROTO_OP_MASK;
		bool enc = (opcode & RTT_PROTO_FLAG_ENC) != 0;
		uint32_t sector = rd_le32(&req_buf[1]);
		uint16_t count = rd_le16(&req_buf[5]);

		switch (op) {
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
			/* Read straight into the response frame's payload area, so the
			 * sector data is never staged in a separate buffer and copied.
			 *
			 * The host always knows `count` (it's the caller's own request),
			 * so it expects a count*SD_SECTOR_SIZE payload back regardless of
			 * status -- zero-fill on error rather than omitting the payload,
			 * so the response length never depends on success/failure. */
			size_t payload_len = (size_t)count * SD_SECTOR_SIZE;
			uint8_t status = RTT_PROTO_STATUS_OK;

			if (rc != 0 || sd_block_read(sector, count, RSP_PAYLOAD) != 0) {
				status = RTT_PROTO_STATUS_ERROR;
				memset(RSP_PAYLOAD, 0, payload_len);
			} else if (enc) {
				/* Decrypt each sector in place, keyed by its absolute
				 * sector number. A locked/failed key fails the whole
				 * request rather than returning ciphertext as if OK. */
				for (uint32_t i = 0; i < count; i++) {
					if (crypto_decrypt_sector(sector + i,
							RSP_PAYLOAD + i * SD_SECTOR_SIZE) != 0) {
						status = RTT_PROTO_STATUS_ERROR;
						memset(RSP_PAYLOAD, 0, payload_len);
						break;
					}
				}
			}
			send_response_in_place(opcode, status, payload_len);
			break;
		}
		case RTT_PROTO_OP_WRITE: {
			if (count == 0 || count > SD_BLOCK_MAX_XFER_SECTORS) {
				send_response(opcode, RTT_PROTO_STATUS_BAD_FRAME, NULL, 0);
				break;
			}
			uint8_t status = RTT_PROTO_STATUS_OK;

			if (enc) {
				/* Encrypt each sector of the request payload in place
				 * before writing it to the card. */
				for (uint32_t i = 0; i < count; i++) {
					if (crypto_encrypt_sector(sector + i,
							&req_buf[7] + i * SD_SECTOR_SIZE) != 0) {
						status = RTT_PROTO_STATUS_ERROR;
						break;
					}
				}
			}
			if (status == RTT_PROTO_STATUS_OK &&
			    (rc != 0 || sd_block_write(sector, count, &req_buf[7]) != 0)) {
				status = RTT_PROTO_STATUS_ERROR;
			}
			send_response(opcode, status, NULL, 0);
			break;
		}
		case RTT_PROTO_OP_UNLOCK: {
			/* count = passphrase length, payload = passphrase bytes. */
			uint8_t status = RTT_PROTO_STATUS_OK;

			if (count == 0 || crypto_unlock(&req_buf[7], count) != 0) {
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
