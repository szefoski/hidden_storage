#ifndef SD_BLOCK_H_
#define SD_BLOCK_H_

#include <stdint.h>
#include <stddef.h>

#define SD_DISK_NAME "SD"
#define SD_SECTOR_SIZE 512U

/* Caps a single READ/WRITE request so the RTT buffers have a fixed max size. */
#define SD_BLOCK_MAX_XFER_SECTORS 64U

/* Initializes the disk_access backend. Returns 0 on success. */
int sd_block_init(void);

/* Fills sector_count/sector_size from the disk. Returns 0 on success. */
int sd_block_get_info(uint32_t *sector_count, uint32_t *sector_size);

int sd_block_read(uint32_t start_sector, uint32_t count, uint8_t *buf);
int sd_block_write(uint32_t start_sector, uint32_t count, const uint8_t *buf);

#endif /* SD_BLOCK_H_ */
