#include "crypto.h"
#include "sd_block.h"

#include <string.h>
#include <zephyr/logging/log.h>
#include <psa/crypto.h>

LOG_MODULE_REGISTER(crypto, LOG_LEVEL_INF);

/* PBKDF2 iteration count. Runs once per unlock (not per sector). Measured at
 * ~8600 iters/s on this device, so this sets the unlock latency: 20000 ~= 2.3s.
 * The iteration count is only a modest work-factor on top of the real defense,
 * which is passphrase strength -- an attacker brute-forcing offline against the
 * card runs the same PBKDF2 on much faster hardware, so use a strong
 * passphrase. Raising this trades unlock latency for a little more brute-force
 * cost. */
#define PBKDF2_ITERATIONS 20000U

/* Fixed derivation salt baked into the firmware.
 *
 * KNOWN LIMITATION: a fixed salt means the same passphrase always yields the
 * same key on every unit running this firmware, so it gives no per-card
 * uniqueness and does not stop precomputation against this build. It is
 * acceptable for a single-user hidden store where the passphrase is the secret,
 * and can be upgraded later to a random salt stored in a reserved card sector
 * without changing the wire protocol. */
static const uint8_t k_salt[16] = {
	0x6e, 0x52, 0x46, 0x35, 0x34, 0x4c, 0x31, 0x35,
	0x2d, 0x68, 0x69, 0x64, 0x64, 0x65, 0x6e, 0x53,
};

static psa_key_id_t m_key_id;
static bool m_unlocked;

int crypto_init(void)
{
	psa_status_t status = psa_crypto_init();

	if (status != PSA_SUCCESS) {
		LOG_ERR("psa_crypto_init failed: %d", (int)status);
		return -1;
	}
	return 0;
}

int crypto_is_unlocked(void)
{
	return m_unlocked;
}

int crypto_unlock(const uint8_t *passphrase, size_t len)
{
	psa_key_derivation_operation_t op = PSA_KEY_DERIVATION_OPERATION_INIT;
	psa_key_attributes_t pass_attr = PSA_KEY_ATTRIBUTES_INIT;
	psa_key_attributes_t aes_attr = PSA_KEY_ATTRIBUTES_INIT;
	psa_key_id_t pass_key = PSA_KEY_ID_NULL;
	psa_key_id_t new_key = PSA_KEY_ID_NULL;
	psa_status_t status;
	int ret = -1;

	if (passphrase == NULL || len == 0) {
		return -1;
	}

	/* The passphrase enters PSA as a DERIVE-type key, so its bytes are handed
	 * to the KDF as the password input rather than living in a plain buffer. */
	psa_set_key_usage_flags(&pass_attr, PSA_KEY_USAGE_DERIVE);
	psa_set_key_algorithm(&pass_attr, PSA_ALG_PBKDF2_HMAC(PSA_ALG_SHA_256));
	psa_set_key_type(&pass_attr, PSA_KEY_TYPE_DERIVE);
	status = psa_import_key(&pass_attr, passphrase, len, &pass_key);
	if (status != PSA_SUCCESS) {
		LOG_ERR("passphrase import failed: %d", (int)status);
		return -1;
	}

	status = psa_key_derivation_setup(&op, PSA_ALG_PBKDF2_HMAC(PSA_ALG_SHA_256));
	if (status != PSA_SUCCESS) {
		LOG_ERR("kdf setup failed: %d", (int)status);
		goto cleanup;
	}
	status = psa_key_derivation_input_integer(&op, PSA_KEY_DERIVATION_INPUT_COST,
						  PBKDF2_ITERATIONS);
	if (status != PSA_SUCCESS) {
		LOG_ERR("kdf cost failed: %d", (int)status);
		goto cleanup;
	}
	status = psa_key_derivation_input_bytes(&op, PSA_KEY_DERIVATION_INPUT_SALT,
						k_salt, sizeof(k_salt));
	if (status != PSA_SUCCESS) {
		LOG_ERR("kdf salt failed: %d", (int)status);
		goto cleanup;
	}
	status = psa_key_derivation_input_key(&op, PSA_KEY_DERIVATION_INPUT_PASSWORD,
					      pass_key);
	if (status != PSA_SUCCESS) {
		LOG_ERR("kdf password failed: %d", (int)status);
		goto cleanup;
	}

	/* Derive straight into a fresh volatile AES key slot: the 256-bit key is
	 * never exposed as bytes, and it disappears on reboot. */
	psa_set_key_usage_flags(&aes_attr, PSA_KEY_USAGE_ENCRYPT | PSA_KEY_USAGE_DECRYPT);
	psa_set_key_lifetime(&aes_attr, PSA_KEY_LIFETIME_VOLATILE);
	psa_set_key_algorithm(&aes_attr, PSA_ALG_CTR);
	psa_set_key_type(&aes_attr, PSA_KEY_TYPE_AES);
	psa_set_key_bits(&aes_attr, 256);
	status = psa_key_derivation_output_key(&aes_attr, &op, &new_key);
	if (status != PSA_SUCCESS) {
		LOG_ERR("kdf output key failed: %d", (int)status);
		goto cleanup;
	}

	/* Swap in the new key only once it is fully derived. */
	if (m_unlocked) {
		psa_destroy_key(m_key_id);
	}
	m_key_id = new_key;
	m_unlocked = true;
	ret = 0;

cleanup:
	psa_key_derivation_abort(&op);
	psa_destroy_key(pass_key);
	return ret;
}

/* CTR IV for a sector: the sector number in the high 64 bits, low 64 bits zero.
 * A 512-byte sector is 32 AES blocks, so CTR increments the low bits 32 times
 * per sector -- staying inside the zeroed low half -- while distinct sectors
 * differ in the high half, so keystreams never overlap between sectors. */
static void sector_iv(uint32_t sector, uint8_t iv[16])
{
	memset(iv, 0, 16);
	iv[0] = (uint8_t)(sector >> 24);
	iv[1] = (uint8_t)(sector >> 16);
	iv[2] = (uint8_t)(sector >> 8);
	iv[3] = (uint8_t)(sector);
}

static int crypt_sector(uint32_t sector, uint8_t *buf, bool encrypt)
{
	psa_cipher_operation_t op = PSA_CIPHER_OPERATION_INIT;
	uint8_t iv[16];
	size_t out_len = 0;
	size_t fin_len = 0;
	psa_status_t status;

	if (!m_unlocked) {
		return -1;
	}

	status = encrypt ? psa_cipher_encrypt_setup(&op, m_key_id, PSA_ALG_CTR)
			 : psa_cipher_decrypt_setup(&op, m_key_id, PSA_ALG_CTR);
	if (status != PSA_SUCCESS) {
		goto fail;
	}

	sector_iv(sector, iv);
	status = psa_cipher_set_iv(&op, iv, sizeof(iv));
	if (status != PSA_SUCCESS) {
		goto fail;
	}

	/* In place: CTR output length equals input length. */
	status = psa_cipher_update(&op, buf, SD_SECTOR_SIZE, buf, SD_SECTOR_SIZE, &out_len);
	if (status != PSA_SUCCESS) {
		goto fail;
	}
	status = psa_cipher_finish(&op, buf + out_len, SD_SECTOR_SIZE - out_len, &fin_len);
	if (status != PSA_SUCCESS) {
		goto fail;
	}
	return 0;

fail:
	LOG_ERR("sector %s failed: %d", encrypt ? "encrypt" : "decrypt", (int)status);
	psa_cipher_abort(&op);
	return -1;
}

int crypto_encrypt_sector(uint32_t sector, uint8_t *buf)
{
	return crypt_sector(sector, buf, true);
}

int crypto_decrypt_sector(uint32_t sector, uint8_t *buf)
{
	return crypt_sector(sector, buf, false);
}
