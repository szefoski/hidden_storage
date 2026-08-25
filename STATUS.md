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
