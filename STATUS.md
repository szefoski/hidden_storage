# Status

## Current state (2026-08-25)

Firmware **builds and links successfully** against NCS v3.4.0 / Zephyr 4.4.0 for
`nrf54l15dk/nrf54l15/cpuapp` (54.5 KB flash, 95 KB RAM used). Not yet flashed or
tested on real hardware — **blocked on the SD card reader/breakout module
delivery.**

Host-side protocol code (`host/protocol.py`) is unit-tested and passing without
hardware (`python3 host/test_protocol.py`). `host/sdrtt.py` (the RTT CLI) is
written but unexercised — needs a flashed board to test against.

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

## How to build / use

See [README.md](README.md) — toolchain setup, build command, flashing, and
host tool usage are all there and confirmed working through the build step.

## Next steps (once the SD card reader arrives)

1. Wire the SD breakout module to the DK's expansion connector (spi22:
   SCK/MOSI/MISO/CS).
2. Update the placeholder pin numbers and `cs-gpios` in
   `firmware/boards/nrf54l15dk_nrf54l15_cpuapp.overlay` to match the actual
   wiring.
3. Flash: `west flash -d firmware/build` (via the `nrfutil sdk-manager
   toolchain launch` wrapper — see README).
4. Run `python3 host/sdrtt.py info` — first real test of the RTT link *and*
   the SD-over-SPI init. If `disk_access_init()` fails (see CMD8/CMD58 note
   above), check wiring/pull-ups before touching firmware logic.
5. Once `info` reports sane sector size/count, test `read`/`write`/`dump`
   round-trips against a throwaway card (or spare sectors) before trusting it
   with real data.
6. Confirm the actual "hidden" property end-to-end: watch the OS's disk/device
   list while the board is connected and RTT traffic is flowing, confirm
   nothing appears.
