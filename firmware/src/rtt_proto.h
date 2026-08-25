#ifndef RTT_PROTO_H_
#define RTT_PROTO_H_

#include <stdint.h>
#include <stddef.h>

/*
 * Binary frame protocol carried over a dedicated SEGGER RTT up/down buffer
 * pair (buffer index RTT_PROTO_CHANNEL), separate from the default RTT
 * console/log channel (buffer 0).
 *
 * Request frame:  [opcode:1][sector:4 LE][count:2 LE][payload...][crc16:2 LE]
 * Response frame: [opcode|0x80:1][status:1][payload...][crc16:2 LE]
 *
 * payload on a READ response is `count * RTT_PROTO_SECTOR_SIZE` bytes of
 * sector data. payload on a WRITE request is the same, sent by the host.
 */

#define RTT_PROTO_CHANNEL 1

#define RTT_PROTO_OP_INFO  0x00
#define RTT_PROTO_OP_READ  0x01
#define RTT_PROTO_OP_WRITE 0x02

#define RTT_PROTO_STATUS_OK    0x00
#define RTT_PROTO_STATUS_ERROR 0x01
#define RTT_PROTO_STATUS_BAD_FRAME 0x02

uint16_t rtt_proto_crc16(const uint8_t *data, size_t len);

/* Initializes the dedicated up/down RTT buffers for the binary protocol. */
void rtt_proto_init(void);

/* Blocks until one full request frame has been read into buf.
 * Returns the number of bytes in the frame, or 0 on a framing/CRC error
 * (caller should just loop and try again). max_len must be large enough for
 * the largest expected request (header + write payload + crc). */
size_t rtt_proto_recv_frame(uint8_t *buf, size_t max_len);

/* Sends one response frame (already including trailing CRC16). */
void rtt_proto_send_frame(const uint8_t *buf, size_t len);

#endif /* RTT_PROTO_H_ */
