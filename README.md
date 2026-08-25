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

J-Link tools (`JLinkExe`, RTT support) must already be installed separately —
[SEGGER's J-Link Software Pack](https://www.segger.com/downloads/jlink/) —
used here for both flashing and the RTT command link.

## Building the firmware

The firmware app lives outside the `~/ncs/v3.4.0` west workspace, so point `west`
at it explicitly. Run this from the repo root -- **the paths must be absolute**:
`--chdir ~/ncs/v3.4.0` changes directory before `west` runs, so a relative
`firmware/build` would resolve against `~/ncs/v3.4.0`, not this repo (and fail
with "source directory firmware does not exist").

```sh
REPO="$(pwd)"
nrfutil sdk-manager toolchain launch --ncs-version v3.4.0 --chdir ~/ncs/v3.4.0 -- \
  west build -b nrf54l15dk/nrf54l15/cpuapp \
  -d "$REPO/firmware/build" \
  "$REPO/firmware"
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

## Before flashing: fix the wiring placeholders

`firmware/boards/nrf54l15dk_nrf54l15_cpuapp.overlay` has placeholder SPI pins
and CS GPIO — **update these to match your actual wiring** from the DK's
expansion connector (spi22) to the SD breakout module, then rebuild (previous
step), before flashing.

(If you're just testing the RTT link/board with no SD card wired up yet,
skip this — the placeholders build and flash fine, `disk_access_init()` just
reports a clean error at runtime, which is expected and already validated.)

## Flashing

Same absolute-path requirement as building:

```sh
REPO="$(pwd)"
nrfutil sdk-manager toolchain launch --ncs-version v3.4.0 --chdir ~/ncs/v3.4.0 -- \
  west flash -d "$REPO/firmware/build"
```

## Host tool

Install into a venv (works everywhere; also the only option on Debian/Ubuntu,
where `pip install` into system Python is blocked by PEP 668):

```sh
python3 -m venv host/.venv
host/.venv/bin/pip install -r host/requirements.txt
```

Everything below except `test_protocol.py` needs `pylink` (i.e. the venv) --
`test_protocol.py` alone has no hardware/pylink dependency and also runs fine
with a plain system `python3`.

```sh
python3 host/test_protocol.py             # hardware-free protocol round-trip tests
host/.venv/bin/python host/test_sdrtt.py  # hardware-free command-logic tests, via a firmware simulator

host/.venv/bin/python host/sdrtt.py info                        # requires flashed board connected via J-Link
host/.venv/bin/python host/sdrtt.py write 1000 -i secret.bin    # write, starting at sector 1000
host/.venv/bin/python host/sdrtt.py read  1000 -o out.bin       # read it back -- size is auto-detected
host/.venv/bin/python host/sdrtt.py dump  -o full_card.img      # whole-card raw image
host/.venv/bin/python host/sdrtt.py restore -i full_card.img    # write a whole-card image back
```

`write` prepends a 12-byte header (`HSD1` magic + 8-byte length) before the
file's bytes, so `read` can recover exactly how many bytes/sectors to fetch
without being told a count. This lives only in `host/protocol.py`
(`build_header`/`parse_header`) — the firmware and the RTT wire protocol
don't know about it, they just move raw sectors. `dump`/`restore` bypass this
entirely for byte-exact whole-card images. For sectors written some other
way, `raw-read <sector> <count> -o file` does an explicit-count read with no
header involved.

Default `--device` is `NRF54L15_M33` — confirmed working against this DK's
on-board J-Link (V9.32). If it isn't accepted by your J-Link version, override
it: `host/.venv/bin/python host/sdrtt.py --device <name> info`.
