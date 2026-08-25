#include "sd_block.h"

#include <zephyr/storage/disk_access.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(sd_block, LOG_LEVEL_INF);

int sd_block_init(void)
{
	int rc = disk_access_init(SD_DISK_NAME);

	if (rc != 0) {
		LOG_ERR("disk_access_init(%s) failed: %d", SD_DISK_NAME, rc);
	}
	return rc;
}

int sd_block_get_info(uint32_t *sector_count, uint32_t *sector_size)
{
	int rc;

	rc = disk_access_ioctl(SD_DISK_NAME, DISK_IOCTL_GET_SECTOR_COUNT, sector_count);
	if (rc != 0) {
		return rc;
	}
	return disk_access_ioctl(SD_DISK_NAME, DISK_IOCTL_GET_SECTOR_SIZE, sector_size);
}

int sd_block_read(uint32_t start_sector, uint32_t count, uint8_t *buf)
{
	return disk_access_read(SD_DISK_NAME, buf, start_sector, count);
}

int sd_block_write(uint32_t start_sector, uint32_t count, const uint8_t *buf)
{
	return disk_access_write(SD_DISK_NAME, buf, start_sector, count);
}
