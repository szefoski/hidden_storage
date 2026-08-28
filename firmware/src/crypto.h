#ifndef CRYPTO_H_
#define CRYPTO_H_

#include <stdint.h>
#include <stddef.h>

/*
 * Optional per-sector payload encryption, backed by the nRF54L15's CRACEN
 * hardware crypto engine through the PSA Crypto API.
 *
 * A passphrase is turned into a 256-bit AES key on-device (PBKDF2-HMAC-SHA256)
 * and held in a volatile PSA key slot for the session only -- never written to
 * flash. Each 512-byte sector is encrypted with AES-256-CTR under an IV derived
 * from its absolute sector number, so the ciphertext is the same size as the
 * plaintext (fits the card 1:1) and any sector can be read or written
 * independently. This provides confidentiality at rest, not integrity: it
 * defends against someone imaging the card, not active tampering.
 */

/* Initializes the PSA crypto subsystem. Call once at startup. Returns 0 on
 * success. */
int crypto_init(void);

/* Derives the session AES key from a passphrase and installs it, replacing any
 * previous key. Returns 0 on success, non-zero on failure (in which case the
 * session is left locked). */
int crypto_unlock(const uint8_t *passphrase, size_t len);

/* True once a session key has been installed by crypto_unlock(). */
int crypto_is_unlocked(void);

/* Encrypt/decrypt one SD_SECTOR_SIZE-byte sector in place, keyed by its
 * absolute sector number. Returns 0 on success, non-zero if locked or on a
 * crypto error. */
int crypto_encrypt_sector(uint32_t sector, uint8_t *buf);
int crypto_decrypt_sector(uint32_t sector, uint8_t *buf);

#endif /* CRYPTO_H_ */
