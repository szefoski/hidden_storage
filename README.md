# Hidden SD Storage via nRF54L15-DK

An nRF54L15-DK sits between a host and a microSD card as a J-Link RTT command
target — not a USB mass-storage device. The card never enumerates on the host,
so the storage is invisible unless you have the board, the J-Link tools, and
this repo's `host/` client. See [PLAN.md](PLAN.md) for the architecture,
[STATUS.md](STATUS.md) for the build log, and
[LESSONS_LEARNED.md](LESSONS_LEARNED.md) for the debugging story behind the
wiring/voltage requirements below.

## Prerequisites

- nRF54L15-DK
- A microSD breakout board + card (tested against the Waveshare 3947)
- [SEGGER J-Link Software Pack](https://www.segger.com/downloads/jlink/) —
  needed on every machine that builds/flashes the firmware *or* runs the host
  tool
- Python 3 (host tool only)
- To build/flash the firmware: [nRF Util](https://files.nordicsemi.com/artifactory/swtools/external/nrfutil/executables/x86_64-unknown-linux-gnu/nrfutil)
  + NCS v3.4.0 (Linux only — see below)

## Wiring the SD card

**First, set the DK's supply to 3.3 V** — it ships at 1.8 V, which is below
the SD spec (2.7-3.6 V) and causes confusing partial failures rather than a
clean error. In nRF Connect for Desktop -> Board Configurator: select the DK,
set VDD to 3.3 V, write, power-cycle. One-time, persists on the DK. Full story
in [LESSONS_LEARNED.md](LESSONS_LEARNED.md).

Then wire SCK/MOSI/MISO/CS/3V3/GND to these DK pins (fixed in
`firmware/boards/nrf54l15dk_nrf54l15_cpuapp.overlay` — override
`cs-gpios`/`pinctrl-0`/`pinctrl-1` there if you wire it differently):

| SPI signal | DK pin | Waveshare 3947 pin |
|---|---|---|
| SCK  | P1.12 | `CLK` |
| MOSI | P1.08 | `CMD` |
| MISO | P1.11 | `D0` |
| CS   | P1.14 | `D3` (labeled `CD/D3`) |
| 3V3  | any VDD pin, P0/P1/P2 header | `VDD`/`3,3V` |
| GND  | any GND pin, P0/P1/P2 header | `VSS`/`GND` |

Avoid P1.04-P1.07 and P0.00-P0.03 — those are the debugger's virtual COM
ports and are physically wired to another chip on the board. The Waveshare
board already has onboard pull-ups; no external resistors needed. No card
wired up yet? The firmware still builds/flashes fine — `sdrtt.py info` just
reports a clean init error until a card is connected.

## Build and flash the firmware (Linux)

```sh
# one-time toolchain install
curl -sL "https://files.nordicsemi.com/artifactory/swtools/external/nrfutil/executables/x86_64-unknown-linux-gnu/nrfutil" -o ~/.local/bin/nrfutil
chmod +x ~/.local/bin/nrfutil
nrfutil install sdk-manager
nrfutil sdk-manager install v3.4.0

# build (run from the repo root; paths must be absolute)
REPO="$(pwd)"
nrfutil sdk-manager toolchain launch --ncs-version v3.4.0 --chdir ~/ncs/v3.4.0 -- \
  west build -b nrf54l15dk/nrf54l15/cpuapp -d "$REPO/firmware/build" "$REPO/firmware"

# flash
nrfutil sdk-manager toolchain launch --ncs-version v3.4.0 --chdir ~/ncs/v3.4.0 -- \
  west flash -d "$REPO/firmware/build"
```

Only tested on Linux. The host tool below is cross-platform and doesn't need
to run on the same machine that flashed the board — any machine with J-Link
software, Python, and USB access to the flashed board can drive it.

## Host tool setup

The host tool (`host/sdrtt.py`) needs the [`pylink-square`](https://pypi.org/project/pylink-square/)
package, installed into a venv (required on Debian/Ubuntu; works everywhere):

**Linux / macOS**
```sh
python3 -m venv host/.venv
host/.venv/bin/pip install -r host/requirements.txt
```

**Windows (PowerShell or cmd)**
```powershell
py -3 -m venv host\.venv
host\.venv\Scripts\pip.exe install -r host\requirements.txt
```

Sanity check, no hardware needed:

```sh
python3 host/test_protocol.py              # Linux/macOS
host/.venv/bin/python host/test_sdrtt.py

py -3 host\test_protocol.py                 # Windows
host\.venv\Scripts\python.exe host\test_sdrtt.py
```

## Using the hidden storage

Same commands on every platform — swap the Python invocation for your OS:
`host/.venv/bin/python` (Linux/macOS) or `host\.venv\Scripts\python.exe`
(Windows).

```sh
sdrtt.py info                        # card info; requires a flashed board over J-Link
sdrtt.py write 1000 -i secret.bin    # write a file, starting at sector 1000
sdrtt.py read  1000 -o out.bin       # read it back — size is auto-detected
sdrtt.py dump  -o full_card.img      # whole-card raw image
sdrtt.py restore -i full_card.img    # write a whole-card image back
```

`sector` also accepts a size offset instead of a raw number — `2M`, `512K`,
`1.5G` — e.g. `sdrtt.py write 2M -i secret.bin` == `sdrtt.py write 4096 -i
secret.bin`. Default `--device` is `NRF54L15_M33`; override with
`--device <name>` if your J-Link version doesn't accept it.

## Encryption (optional)

`-e`/`--encrypt` on `write`/`read` encrypts the payload on-device (AES-256-CTR
via CRACEN hardware crypto), keyed from a passphrase — the SD card only ever
stores ciphertext.

```sh
SDRTT_PASSPHRASE='correct horse battery staple' sdrtt.py write 2M -e -i secret.bin
SDRTT_PASSPHRASE='correct horse battery staple' sdrtt.py read  2M -e -o out.bin
# or omit SDRTT_PASSPHRASE / --passphrase for an interactive prompt
```

This protects confidentiality (the passphrase is the real defense — use a
strong one) but not integrity — there's no tamper detection. Full threat
model and design in [ENCRYPTION_PLAN.md](ENCRYPTION_PLAN.md).

## Notes

- Sector size is 512 bytes (SD/SDHC/SDXC standard); the tool self-checks this
  against what the card reports and refuses to proceed if they disagree.
- `write`/`read` prepend a small header so `read` knows the byte count
  automatically; `raw-read <sector> <count> -o file` skips that for sectors
  written some other way, and `dump`/`restore` always work byte-exact with no
  header.
- Transfers over 32 sectors (16 KB) print a progress bar to stderr.
