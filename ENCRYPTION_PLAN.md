# Encrypt SD data at rest using the nRF54L15's CRACEN + KMU hardware

## Context

The card is opaque to the OS already (no USB MSC), but data on the card is
currently stored in the clear -- pulling the card and reading it directly
with any SD reader recovers everything. The user asked whether the board has
security hardware worth using: it does. The nRF54L15 has **CRACEN**, a
hardware crypto engine (AES, SHA, ECC, TRNG) exposed through Zephyr's PSA
Crypto API, and a **KMU** (Key Management Unit) that can hold an AES key in a
hardware-protected slot -- CRACEN can use it for encrypt/decrypt, but
firmware code (and therefore anyone reading firmware/flash) can never read
the key back out. This closes the "pull the card and read it" gap using a
real hardware-rooted guarantee rather than a key hidden in source code.

Decisions already made with the user (via AskUserQuestion):
- **Key**: AES-256, generated on-device from CRACEN's hardware TRNG on first
  boot, stored in a KMU **protected** slot (non-extractable). No external
  provisioning tooling -- sidesteps the NCS 3.4.0 provisioning-tool issues
  found in research.
- **Key loss = data loss.** No backup/export path. Matches "hidden storage,"
  not "password-protected storage."
- **Encryption is transparent at the SD boundary in firmware.** RTT wire
  protocol and `host/` stay completely unchanged -- host still sends/receives
  plaintext; only the SD card ever sees ciphertext. This also means the
  plaintext length header (`HSD1` magic, see `host/protocol.py`) gets
  encrypted along with everything else once written to the card -- no fixed
  recognizable bytes at rest.

Cipher choice: **AES-256-CTR with a deterministic per-sector IV** (derived
from the absolute sector number, not stored). This keeps ciphertext exactly
the same size as plaintext -- no on-disk format/layout change, no interaction
with the existing chunking or the host's auto-length-header design. This is
the same tradeoff real full-disk encryption (BitLocker, dm-crypt) makes:
confidentiality against extraction, not tamper-detection (no auth tag). Worth
documenting, not solving here -- out of scope for "hidden storage."

## Research findings (verified, not guessed)

- CRACEN is automatically available via NCS's `nrf_security` module once PSA
  Crypto is enabled -- it's already the SoC's only entropy source, so parts
  of this stack are already linked into our build (`cracen_psa_driver.a` etc
  were already visible in earlier build logs).
- Kconfig: `CONFIG_PSA_CRYPTO=y`, `CONFIG_PSA_WANT_ALG_CTR=y`,
  `CONFIG_PSA_WANT_KEY_TYPE_AES=y`, `CONFIG_PSA_WANT_AES_KEY_SIZE_256=y`.
  KMU-specific symbols (something under the CRACEN/KMU key-location Kconfig
  tree) need to be confirmed against the actual installed source at
  `~/ncs/v3.4.0` during implementation -- same discipline as the disk-access
  Kconfig bugs found earlier: **grep the real Kconfig files, don't guess
  symbol names.**
- KMU protected-key API shape (from Nordic docs, to be cross-checked against
  the real sample below):
  ```c
  psa_key_attributes_t attr = PSA_KEY_ATTRIBUTES_INIT;
  psa_set_key_type(&attr, PSA_KEY_TYPE_AES);
  psa_set_key_bits(&attr, 256);
  psa_set_key_algorithm(&attr, PSA_ALG_CTR);
  psa_set_key_usage_flags(&attr, PSA_KEY_USAGE_ENCRYPT | PSA_KEY_USAGE_DECRYPT);
  psa_set_key_lifetime(&attr, PSA_KEY_LIFETIME_FROM_PERSISTENCE_AND_LOCATION(
      CRACEN_KEY_PERSISTENCE_REVOKABLE, PSA_KEY_LOCATION_CRACEN_KMU));
  psa_set_key_id(&attr, PSA_KEY_ID_FROM_CRACEN_KMU_SLOT(
      CRACEN_KMU_KEY_USAGE_SCHEME_PROTECTED, SLOT_NR));
  psa_generate_key(&attr, &key_id);   // first boot
  // later boots: psa_get_key_attributes(key_id, &attr) succeeds -> key exists, reuse key_id directly
  ```
- **Explicit deterministic IV requires the PSA multipart cipher API**, not
  the one-shot `psa_cipher_encrypt()`/`psa_cipher_decrypt()` (those
  auto-generate a random IV and prepend it to the output -- wrong for our
  scheme, since we derive the IV from the sector number and never store it).
  Use `psa_cipher_encrypt_setup()` / `psa_cipher_set_iv()` /
  `psa_cipher_update()` / `psa_cipher_finish()` (and the `_decrypt_setup`
  equivalent), once per 512-byte sector, with a 16-byte IV built from the
  absolute sector index.
- Official reference sample to crib the exact working API calls from (read
  it during implementation rather than trusting memory):
  `~/ncs/v3.4.0/nrf/samples/crypto/kmu_cracen_usage/src/` (also
  `persistent_key_usage` for the general persistent-key pattern).
- Important operational gotcha to document prominently: a normal
  `west flash` (program-only) does **not** erase KMU slots -- the key
  survives firmware re-flashes. A full chip erase/recover
  (`nrfjprog --recover`, `west flash --erase`, etc.) destroys it
  irrecoverably, taking all card data with it. This must be called out
  loudly in the docs given "key loss = data loss" was the chosen design.

## Implementation

New file `firmware/src/crypto.c` / `.h`:
- `crypto_init(void)` -- calls `psa_crypto_init()`, then checks whether the
  fixed KMU slot's key already exists (`psa_get_key_attributes()` on the
  fixed `key_id`); if not, generates a new AES-256 key into that slot via
  `psa_generate_key()`. Logs (via `LOG_INF`) whether a key was freshly
  generated or an existing one was found -- this is the only user-visible
  signal, no RTT protocol change needed.
- `crypto_selftest(void)` -- encrypts and decrypts a small known plaintext
  block in RAM using the real key/CRACEN path and asserts round-trip
  equality, logging PASS/FAIL. This is the key piece of interim
  verification: it exercises CRACEN + KMU end-to-end *without* needing the
  SD card, so we can prove the crypto hardware actually works on this
  specific board today, while still waiting on the SD reader.
- `crypto_encrypt_sectors(uint32_t start_sector, uint8_t *buf, size_t count)`
  and `crypto_decrypt_sectors(...)` -- iterate per 512-byte block, deriving
  a 16-byte IV from `start_sector + i` (e.g. big-endian sector index in the
  first 4 bytes, zero elsewhere), transforming in place using the multipart
  PSA cipher API described above.

`firmware/src/sd_block.c` changes:
- `sd_block_write()`: call `crypto_encrypt_sectors()` on the buffer in place
  before `disk_access_write()`.
- `sd_block_read()`: call `crypto_decrypt_sectors()` on the buffer in place
  after `disk_access_read()` returns.
- Confirm in-place transform (input pointer == output pointer) works with
  the PSA multipart CTR path during implementation; fall back to a 512-byte
  scratch buffer per sector if not.

`firmware/src/main.c`: call `crypto_init()` then `crypto_selftest()` once at
startup, before `sd_block_init()`/`rtt_proto_init()`. Log results either way;
don't block boot on self-test failure (still useful to see the RTT link and
report the failure clearly).

`firmware/CMakeLists.txt`: add `src/crypto.c` to `target_sources`.

`firmware/prj.conf`: add the PSA/CRACEN/KMU Kconfig symbols above, with the
KMU-location symbol name confirmed against the real source tree before
committing to it (same approach that caught the `DISK_ACCESS` /
`SDMMC_STACK` mistakes earlier in this project).

## Documentation updates

- `STATUS.md`: new discoveries (CRACEN/KMU Kconfig symbols, PSA API shape,
  the in-place-transform question, whatever the actual build/flash run
  surfaces), and update "current state" once the self-test result is known.
- `README.md`: explain the encryption is transparent (nothing changes in how
  you call `sdrtt.py`), and prominently document the erase/recover warning
  and the "key loss = permanent data loss" tradeoff.

## Verification

1. Build (`west build`) -- resolve any Kconfig/API mismatches the same way
   prior ones were resolved: read the actual installed NCS source, don't
   guess.
2. Flash to the already-connected nRF54L15-DK.
3. Watch the RTT/console log (`JLinkRTTClient` or the existing log backend)
   for the `crypto_init` key-generation message and the `crypto_selftest`
   PASS/FAIL result -- this proves CRACEN + KMU actually work on this board,
   independent of the SD card.
4. Re-run `sdrtt.py info` / `write` / `read` -- should still report a clean
   `ERROR` (no SD card yet), confirming no regression in the existing,
   already-validated RTT command path.
5. Full encrypt-at-rest round-trip (write plaintext, dump raw sectors,
   confirm they're not the plaintext, read back and confirm it matches) is
   deferred until the SD card arrives -- added to `STATUS.md`'s next-steps
   list alongside the existing SPI wiring steps.
