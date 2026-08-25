# Hidden SD Storage via nRF54L15-DK

See [PLAN.md](PLAN.md) for the full architecture and design rationale.

## Toolchain setup (done once)

Installed via nRF Util (Nordic's current official installer):

```sh
curl -sL "https://files.nordicsemi.com/artifactory/swtools/external/nrfutil/executables/x86_64-unknown-linux-gnu/nrfutil" -o ~/.local/bin/nrfutil
chmod +x ~/.local/bin/nrfutil
nrfutil install sdk-manager
nrfutil sdk-manager install v3.4.0   # NCS v3.4.0 (Zephyr 4.4), installs to ~/ncs/v3.4.0
```

J-Link tools (`JLinkExe`, RTT support) must already be installed separately (SEGGER's J-Link Software Pack) — used here for both flashing and the RTT command link.

## Building the firmware

The firmware app lives outside the `~/ncs/v3.4.0` west workspace, so point `west`
at it explicitly:

```sh
nrfutil sdk-manager toolchain launch --ncs-version v3.4.0 --chdir ~/ncs/v3.4.0 -- \
  west build -b nrf54l15dk/nrf54l15/cpuapp \
  -d firmware/build \
  firmware
```

Confirmed working (NCS v3.4.0 / Zephyr 4.4.0): links cleanly at 54.5 KB flash /
95 KB RAM. Two Kconfig gotchas found while getting this to build clean, already
fixed in `firmware/prj.conf`:

- The disk_access API (`disk_access_read/write/ioctl/init`) lives behind
  `CONFIG_DISK_ACCESS`, not `CONFIG_DISK_DRIVERS` (which only pulls in the
  low-level per-backend drivers and is auto-selected by `DISK_ACCESS` anyway).
- The SD-over-SPI + SDMMC disk stack needs `CONFIG_SDMMC_STACK=y` (which
  selects `CONFIG_SDHC`); `CONFIG_DISK_DRIVER_SDMMC` and `CONFIG_SPI_SDHC`
  then default to `y` on their own once the devicetree overlay's
  `zephyr,sdmmc-disk` / `zephyr,sdhc-spi-slot` nodes are present.

## Flashing

```sh
nrfutil sdk-manager toolchain launch --ncs-version v3.4.0 --chdir ~/ncs/v3.4.0 -- \
  west flash -d firmware/build
```

## Before flashing: fix the wiring placeholders

`firmware/boards/nrf54l15dk_nrf54l15_cpuapp.overlay` has placeholder SPI pins
and CS GPIO — **update these to match your actual wiring** from the DK's
expansion connector (spi22) to the SD breakout module before flashing.

## Host tool

```sh
pip install -r host/requirements.txt
python3 host/test_protocol.py     # hardware-free protocol round-trip tests
python3 host/sdrtt.py info        # requires flashed board connected via J-Link
```

Default `--device` is `NRF54L15_M33` — confirmed working against this DK's
on-board J-Link (V9.32). If it isn't accepted by your J-Link version, override
it: `python3 host/sdrtt.py --device <name> info`.

On Debian/Ubuntu, `pip install` into system Python is blocked (PEP 668) — use
a venv: `python3 -m venv host/.venv && host/.venv/bin/pip install -r host/requirements.txt`.
