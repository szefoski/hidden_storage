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

## Wiring the SD card

`firmware/boards/nrf54l15dk_nrf54l15_cpuapp.overlay` uses `&spi22`
("nordic_expansion_spi" in Nordic's own board file) with its **stock default
pin assignment** -- no pins invented or guessed here; verified directly
against `nrf54l15dk_nrf54l_05_10_15-pinctrl.dtsi` and
`nrf54l_05_10_15_cpuapp_common.dtsi` in the installed NCS source, and
cross-checked against the nRF54L15 DK Hardware User Guide's pin maps.
(An earlier version of this overlay had placeholder pins that turned out to
collide with onboard buttons/LEDs -- fixed by just using Nordic's own
already-correct default instead of inventing one.)

| Signal | DK pin | Note |
|---|---|---|
| SCK  | P1.11 | Free GPIO, no other function |
| MISO | P1.07 | Shared with UART1_RXD -- freed by disabling `&uart30` (this overlay already does that; spi22 and UART30 share these pins) |
| MOSI | P1.06 | Shared with UART1_RST -- same as above |
| CS   | P1.10 | Nordic's own default CS pin for this node. Also wired to LED1, but LEDs on this DK are buffered through a transistor, not driven directly -- so this is a harmless cosmetic side effect (LED1 flickers with SPI CS activity), not an electrical conflict |
| 3V3  | any VDDIO/VCC pin on the P0/P1/P2 header | see the DK's silkscreen |
| GND  | any GND pin on the P0/P1/P2 header | see the DK's silkscreen |

Wire your SD breakout module's SCK/MISO/MOSI/CS/3V3/GND to those DK pins,
then build and flash (previous/next sections) -- no overlay changes needed
for the default wiring above. If you wire it differently, override
`cs-gpios`/`pinctrl-0`/`pinctrl-1` in the overlay to match.

(If you're just testing the RTT link/board with no SD card wired up yet,
none of this matters yet -- the overlay builds and flashes fine either way,
`disk_access_init()` just reports a clean error at runtime, which is
expected and already validated.)

## Flashing

Same absolute-path requirement as building:

```sh
REPO="$(pwd)"
nrfutil sdk-manager toolchain launch --ncs-version v3.4.0 --chdir ~/ncs/v3.4.0 -- \
  west flash -d "$REPO/firmware/build"
```

## Host tool

The `host/` scripts (`sdrtt.py`, `protocol.py`, `fake_firmware.py`) are pure
Python and platform-agnostic -- all file I/O goes through `pathlib.Path`, and
the only third-party dependency is [`pylink-square`](https://pypi.org/project/pylink-square/),
which SEGGER's own tooling recommends and which ships official wheels for
Linux, macOS, *and Windows*. The firmware/build/flash steps above are
Linux-only as documented (that's what this dev machine runs), but the host
tool itself doesn't need to run on the same machine that built/flashed the
firmware -- once a board is flashed, anything with J-Link software, Python,
and USB access to the board can drive it.

Requires [SEGGER's J-Link Software Pack](https://www.segger.com/downloads/jlink/)
installed on whichever machine runs these (same requirement as above, any OS).

### Linux / macOS

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

### Windows

Same tools, same commands -- just Windows paths/venv layout. **Not tested on
an actual Windows machine** (this project was built entirely on Linux); this
is a direct translation of the Linux instructions, not independently verified.
None of these commands are PowerShell- or cmd-specific (no `$env:`, no
line-continuation), so both shells below use identical lines -- provided
separately anyway so each is directly copy-pasteable without translating.

**PowerShell:**

```powershell
py -3 -m venv host\.venv
host\.venv\Scripts\pip.exe install -r host\requirements.txt

py -3 host\test_protocol.py                      # hardware-free, no venv needed
host\.venv\Scripts\python.exe host\test_sdrtt.py  # hardware-free command-logic tests

host\.venv\Scripts\python.exe host\sdrtt.py info
host\.venv\Scripts\python.exe host\sdrtt.py write 1000 -i secret.bin
host\.venv\Scripts\python.exe host\sdrtt.py read  1000 -o out.bin
host\.venv\Scripts\python.exe host\sdrtt.py dump  -o full_card.img
host\.venv\Scripts\python.exe host\sdrtt.py restore -i full_card.img
```

**cmd.exe:**

```bat
py -3 -m venv host\.venv
host\.venv\Scripts\pip.exe install -r host\requirements.txt

py -3 host\test_protocol.py
host\.venv\Scripts\python.exe host\test_sdrtt.py

host\.venv\Scripts\python.exe host\sdrtt.py info
host\.venv\Scripts\python.exe host\sdrtt.py write 1000 -i secret.bin
host\.venv\Scripts\python.exe host\sdrtt.py read 1000 -o out.bin
host\.venv\Scripts\python.exe host\sdrtt.py dump -o full_card.img
host\.venv\Scripts\python.exe host\sdrtt.py restore -i full_card.img
```

If `pylink`/the J-Link DLL fails to load, `sdrtt.py` now prints a direct
pointer to the J-Link Software Pack download instead of pylink's raw error
(same fix applies on Linux/macOS).

### Usage notes (all platforms)

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
it: `sdrtt.py --device <name> info`.

**Sector size is 512 bytes**, the SD/SDHC/SDXC standard, hardcoded as
`SECTOR_SIZE` in `host/protocol.py` and `SD_SECTOR_SIZE` in
`firmware/src/sd_block.h`. You can check what a card actually reports via
`sdrtt.py info`'s `sector_size=` output. Before any `read`/`write`/
`raw-read`/`dump`/`restore`, the tool also queries this itself and refuses
to proceed if it ever disagrees with the hardcoded value (a real card
reporting something other than 512 essentially never happens with SD
hardware, but silently doing the wrong sector-boundary math would be worse
than refusing outright).

**`sector` accepts a size offset instead of a raw sector number** on
`read`/`write`/`raw-read` — `2M`, `512K`, `1G` (decimals allowed, e.g.
`1.5M`, case-insensitive, optional trailing `B`), converted to a sector
number before anything else runs. `sdrtt.py write 2M -i secret.bin` is
exactly `sdrtt.py write 4096 -i secret.bin`. An offset that isn't an exact
multiple of 512 bytes is rejected with the nearest valid sector numbers on
either side, rather than silently rounding.

Any `write`/`read`/`raw-read`/`dump`/`restore` transfer spanning more than
one RTT request (>32 sectors, 16KB) prints a live progress bar to stderr
(`[####------] 42% (21/50 sectors)`) so a large `dump` doesn't sit silent.
Small transfers that finish in a single request don't show one -- not worth
the noise.
