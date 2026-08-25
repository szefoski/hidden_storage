# Status

## Current state (2026-08-25)

Firmware **builds, flashes, and runs on the real nRF54L15-DK.** RTT link to
the host is confirmed working end-to-end: `sdrtt.py info` connects, sends a
framed request, and gets back a correctly-CRC'd response. It reports
`STATUS_ERROR` because no SD card is wired up yet — that's the expected,
correct behavior (`disk_access_init()` fails cleanly, firmware surfaces the
error instead of hanging).

Also confirmed the actual point of the project: with the board flashed and
RTT traffic flowing, `lsusb` shows only the J-Link debug probe
(`1366:1069 SEGGER J-Link`) — no mass-storage or other device appears. This
was checked with no SD card present, so it's not full confirmation (the
concern would be if enumerating an SD card via SPI at runtime somehow
triggered a new device — it can't, since there's no USB device stack in this
firmware at all, but worth a final sanity check once the card is wired up
too).

**Blocked on the SD card reader/breakout module delivery** for the SPI/SD
electrical bring-up (wiring, pin numbers in the overlay, and the actual
`disk_access_init()` success path).

Host-side protocol code (`host/protocol.py`) is unit-tested and passing
without hardware (`python3 host/test_protocol.py`, 9 tests). `host/sdrtt.py`
(the RTT CLI) is confirmed working against real hardware for `info`, `write`,
and `read` -- all three round-trip correctly over RTT and report the expected
clean `ERROR` status (no SD card wired up yet, so `disk_access_*` calls fail,
but the framing/CRC/chunking/error-propagation all work). `dump`/`restore`
are written but not yet exercised on hardware.

**`host/fake_firmware.py`** is a hardware-free double for the firmware's
frame-handling logic (`FakeFirmware`, mirroring `rtt_proto.c` + `main.c`'s
dispatch, as a second independent implementation, not generated from the C).
It exposes the same `rtt_write`/`rtt_read` interface `sdrtt.py` calls on a
real `pylink.JLink`, so it can stand in for the board entirely.
`host/test_sdrtt.py` (8 tests, all passing) exercises `info`/`write`/`read`/
`raw-read`/`dump`/`restore` through it, including multi-chunk transfers
(>32 sectors, forcing multiple RTT requests) and a regression test for the
exact READ-response hang bug found on real hardware. Caveat documented in
`fake_firmware.py`'s docstring: it's hand-maintained, not derived from the C,
so it won't automatically catch a future firmware regression unless the
double is updated to match -- it's real coverage for the host-side chunking/
header/error-propagation logic, not a substitute for hardware testing.

`write` now embeds a small length header (`host/protocol.py`:
`build_header`/`parse_header`, host-side only, not part of the wire protocol)
so `read` auto-detects how many bytes/sectors to fetch instead of requiring
an explicit count. `dump`/`restore` stay header-free for byte-exact whole-card
images; `raw-read` is the explicit-count escape hatch for sectors not written
via `write`.

**Real bug found and fixed while first exercising `read` on hardware:** the
firmware's READ response omitted the payload entirely on error, but the host
always expects a response sized to the sector count *it* requested --
mismatched lengths made `read` hang until timeout instead of failing cleanly.
Fixed in `firmware/src/main.c` by zero-filling and sending the full-size
payload on error too (same pattern `INFO`'s response already used). This is
the kind of bug that's easy to miss without hardware in the loop -- `write`
and `info` happened to not hit it, since their response sizes were already
status-independent.

**Windows portability pass:** `host/*.py` were audited against the pattern
used in the sibling `nRF54L15-storage` project (a related on-chip-RRAM
storage tool of the user's) -- adopted `pathlib.Path` for all file I/O and
added a clearer connection-failure error message pointing at the J-Link
Software Pack download. Deliberately did *not* adopt that sibling project's
"avoid pylink, shell out to JLinkExe directly" approach: that works for their
tool because it only needs flash `loadfile`/`savebin`, which JLinkExe's CLI
scripting handles fine, but our RTT-based bidirectional binary protocol has
no clean CLI-scriptable equivalent (would mean hand-rolling the SEGGER RTT
ring-buffer protocol via raw memory pokes). `pylink-square` already ships
official Windows wheels, so it isn't actually the portability gap here.
README now has a Windows/PowerShell command block alongside the Linux/macOS
one -- **not tested on real Windows**, direct translation only.

**Sector-size safety check + byte/MB offset addressing:** `sdrtt.py` now
queries the card's real sector size via `INFO` before any
`read`/`write`/`raw-read`/`dump`/`restore` and refuses to proceed if it ever
disagrees with the hardcoded 512-byte `SECTOR_SIZE` constant, rather than
silently doing the wrong sector-boundary math. Chose validate-and-fail-loudly
over threading a dynamic sector size through firmware buffers/RTT
framing/host chunking -- real SD/SDHC/SDXC cards are universally 512-byte
block-addressed by spec, so full adaptation isn't worth the added surface
for a case that essentially never happens. Also: `sector` on
`read`/`write`/`raw-read` now accepts a size-suffixed offset (`2M`, `512K`,
`1G`) as an alternative to a raw sector number
(`protocol.parse_sector_or_offset`), converted before anything else runs;
a non-sector-aligned offset is rejected with the nearest valid sector
numbers rather than silently rounded. 29 tests passing (up from 19).

## Discoveries / gotchas found along the way

- **Kconfig for disk access is `CONFIG_DISK_ACCESS`, not `CONFIG_DISK_DRIVERS`.**
  `DISK_DRIVERS` only builds the low-level per-backend drivers; the
  `disk_access_read/write/ioctl/init()` API firmware actually calls
  (`firmware/src/sd_block.c`) lives behind `CONFIG_DISK_ACCESS`, which selects
  `DISK_DRIVERS` itself. Confirmed by reading
  `~/ncs/v3.4.0/zephyr/subsys/disk/Kconfig`.
- **SD-over-SPI needs `CONFIG_SDMMC_STACK=y`** to pull in the SD command stack
  (it selects `CONFIG_SDHC`). Once that's on, `CONFIG_DISK_DRIVER_SDMMC` and
  `CONFIG_SPI_SDHC` default to `y` on their own, gated by whether the
  devicetree overlay actually has `zephyr,sdmmc-disk` / `zephyr,sdhc-spi-slot`
  nodes present.
- **`&spi22` (the DK's `nordic_expansion_spi` label) conflicts with UART30's
  default pin assignment** on the nRF54L15-DK — the devicetree overlay
  disables `&uart30` to free those pins.
- Nordic DevZone has open reports of SD-over-SPI init failures specifically on
  nRF54L15 (CMD8 timeout → "Legacy card detected", CMD58 "No OCR" →
  `disk_access_init()` returns -134). Environment/wiring/card-dependent —
  something to watch for once real hardware is in the loop, not something
  fixed preemptively in code.
- Installed via **nRF Util** (`nrfutil sdk-manager`), Nordic's current
  official CLI installer — see [README.md](README.md) for exact commands.
  Needed to free ~5GB (pip cache) first on this machine before the SDK +
  toolchain download would fit.
- The J-Link device name for pylink's `jlink.connect()` is **`NRF54L15_M33`**,
  not `NRF54L15_XXAA` as originally guessed — found by probing with
  `JLinkExe` directly. `host/sdrtt.py`'s default is updated to match.
- `pip install` into system Python is blocked by Debian/Ubuntu's PEP 668
  guard; use a venv for the host tool (`host/.venv`, see README).

## How to build / use

See [README.md](README.md) — toolchain setup, build command, flashing, and
host tool usage are all there and confirmed working through the build step.

## Next steps (once the SD card reader arrives)

1. Wire the SD breakout module to the DK's expansion connector (spi22:
   SCK/MOSI/MISO/CS).
2. Update the placeholder pin numbers and `cs-gpios` in
   `firmware/boards/nrf54l15dk_nrf54l15_cpuapp.overlay` to match the actual
   wiring.
3. Re-flash (same command as before, already confirmed working):
   `nrfutil sdk-manager toolchain launch --ncs-version v3.4.0 --chdir ~/ncs/v3.4.0 -- west flash -d firmware/build`
4. Run `python3 host/sdrtt.py info` — RTT link is already proven working; this
   run is specifically to test SD-over-SPI init. If `disk_access_init()`
   fails (see CMD8/CMD58 note above), check wiring/pull-ups before touching
   firmware logic.
5. Once `info` reports sane sector size/count, test `read`/`write`/`dump`
   round-trips against a throwaway card (or spare sectors) before trusting it
   with real data.
6. Re-check `lsusb` with the card wired up and RTT traffic flowing, to
   confirm nothing new appears now that real SPI/SD activity is happening
   (expected: still just the J-Link probe, per the reasoning above).

## Planned next improvement: encrypt data at rest (not started)

Once raw read/write against a real card is confirmed working end-to-end (the
steps above), the next improvement is encrypting data at rest using the
nRF54L15's on-chip security hardware — **CRACEN** (hardware AES/SHA/ECC/TRNG,
exposed via Zephyr's PSA Crypto API) and **KMU** (hardware key storage that
can hold an AES key CRACEN can use for encrypt/decrypt without firmware ever
being able to read the key back out).

Full design — cipher choice (AES-256-CTR, per-sector deterministic IV), key
provisioning (generated on-device from CRACEN's TRNG into a KMU protected
slot, no export path — key loss means data loss, by design), the exact PSA
API calls involved, and a step-by-step implementation/verification plan — is
written up in [ENCRYPTION_PLAN.md](ENCRYPTION_PLAN.md). Deliberately not
started yet: layering hardware-backed crypto onto a data path that hasn't
seen a real SD card would make it hard to tell which layer broke if
something failed. Pick this up after the raw-storage path above is solid.

## Planned next improvement: store a git repository on the hidden storage (not started)

Also queued for after the raw-storage path is confirmed: a host-side wrapper
(`host/git_hidden.py`, no firmware changes) that uses `git bundle` --
git's own format for offline/removable-media transport -- around the
existing `write`/`read` commands. `push <sector>` snapshots refs into a
bundle and writes it (full-state replace, not incremental); `fetch`/`clone
<sector>` reads the bundle back and lets git unpack it natively, since git
already treats a bundle file as a valid fetch/clone source. Full design in
[GIT_STORAGE_PLAN.md](GIT_STORAGE_PLAN.md). Chosen over a full custom
`git-remote-helper` protocol implementation -- our storage is a dumb blob
store with no live git process on the other end, so push would be a
whole-bundle replace either way; the extra protocol work would only buy UX
polish (`git push card` instead of `git_hidden.py push <sector>`), not real
incremental transfer.
