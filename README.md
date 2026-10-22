<p align="center">
  <img src="custom_components/iqair_healthpro/brand/icon@2x.png" alt="IQAir icon" width="128" height="128">
</p>
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/iqair_healthpro/brand/dark_logo@2x.png">
    <img src="custom_components/iqair_healthpro/brand/logo@2x.png" alt="IQAir" width="320">
  </picture>
</p>

# IQAir HealthPro (BLE) for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)

A **local** Home Assistant integration for the **IQAir HealthPro Plus** air purifier
(developed against the *HealthPro Plus XE*). It talks to the purifier directly over
Bluetooth, reading air quality and filter life and controlling power, fan speed, Auto
mode and the display light. **No IQAir cloud, no account.**

It uses HA's Bluetooth stack, so it connects through the host adapter **or any ESPHome
Bluetooth proxy**, whichever hears the purifier best.

> [!NOTE]
> This is an unofficial, community-built integration. It is not affiliated with or
> endorsed by IQAir. Use at your own risk. The IQAir name and logo are trademarks of
> IQAir; they're bundled in `brand/` only so Home Assistant shows the right icon.

## Features

- Particulates: PM1, PM2.5 and PM10 (µg/m³) from the purifier's own sensor
- A fan entity with on/off, 6 speed levels, and an **Auto** preset (the purifier's
  sensor-driven mode). Picking a speed turns Auto off, as on the unit itself.
- Filter life for the HyperHEPA, pre-filter and gas & odor filters
- Fan speed (%), fan RPM, clean air delivery rate, and lifetime counters (air cleaned,
  powered time, fan run time)
- Display light and Auto mode switches, an **Identify** button, a **Problem** sensor
  (the purifier's error code), and a **Connected** sensor
- Diagnostics download with every raw dataPoint, useful for supporting other models

## Entities

| Entity | Type | Notes |
| --- | --- | --- |
| *(device name)* | fan | Power, speed levels 1–6 (17/33/50/67/83/100%), preset `auto` |
| PM1 / PM2.5 / PM10 | sensor (µg/m³) | |
| Fan speed | sensor (%) | Actual motor output, including while in Auto |
| Fan RPM | sensor | |
| Clean air delivery rate | sensor (m³/h) | |
| HyperHEPA / Pre-filter / Gas & odor filter life | sensor (%) | |
| Total air cleaned | sensor (m³) | Lifetime, `total_increasing` |
| Total powered time / Total fan run time | sensor (h) | Diagnostic |
| Wi-Fi signal | sensor (dBm) | Diagnostic, disabled by default |
| Auto mode | switch | Same as the fan's `auto` preset |
| Display light | switch | Config |
| Identify | button | Diagnostic |
| Problem | binary_sensor | `on` when the error code is non-zero; code in attributes |
| Connected | binary_sensor | `on` while the BLE session is up and polls succeed |

## Requirements

- Home Assistant 2024.8.0 or newer
- A Bluetooth adapter or proxy in range that can make **connections**: an ESPHome
  Bluetooth proxy (recommended, pairs automatically) or the HA host's own adapter.
  Passive-only proxies (e.g. Shelly devices, or an ESPHome proxy with `active: false`)
  can see the purifier but can't connect to it.
- Nothing else holding the purifier's Bluetooth connection (e.g. a phone with the IQAir
  app open over Bluetooth). The app keeps working over Wi-Fi/cloud.

## Installation

### HACS (recommended)

1. In HACS → **Integrations** → ⋮ → **Custom repositories**, add
   `https://github.com/dipseth/iqair_healthpro_ble` as an **Integration**.
2. Install **IQAir HealthPro (BLE)** and restart Home Assistant.

### Manual

Copy `custom_components/iqair_healthpro` into your Home Assistant
`config/custom_components/` directory and restart.

## Setup

HA should **auto-discover** the purifier. It advertises a coded name such as
`NQQHN50NQONWTNFIN-P`, not "IQAir", so it's matched on IQAir's Bluetooth manufacturer ID.
Confirm the discovery, or add it via **Settings → Devices & Services → Add Integration →
IQAir HealthPro (BLE)**. Setup connects once to check the link and names the entry after
the model the purifier reports (e.g. *IQAir HealthPro Plus L002-4*).

### Pairing (bonding)

The purifier only answers over an **encrypted, paired** link, and the pairing is stored
in each Bluetooth adapter, not in Home Assistant.

- **ESPHome proxy: automatic.** When the purifier refuses the first read, the
  integration asks the proxy to pair. The ESP32 pairs without a code in about 150 ms,
  then everything works. No action needed.
- **HA host adapter (BlueZ): pair once by hand.** BlueZ needs the purifier's 6-digit
  Bluetooth pairing code. From the *Advanced SSH & Web Terminal* add-on:

  ```text
  bluetoothctl
  agent KeyboardOnly
  default-agent
  pair AA:BB:CC:DD:EE:FF        # the purifier's address
  # enter the 6-digit code when asked for the passkey
  trust AA:BB:CC:DD:EE:FF
  quit
  ```

  The link has to be solid for this. A weak signal (around -85 dBm) drops mid-pairing,
  so move closer or use a proxy.

If several radios hear the purifier but only one is paired, pin the integration to it
under **Configure → Bluetooth adapter / proxy**. The same screen sets the **poll
interval** (default 30 s).

If pairing is missing, setup fails with *"this Bluetooth adapter is not paired"* and the
log explains what to do.

## How it works

The purifier exposes one custom GATT service (`1b5ae7e4-…`) with a write characteristic
(TX `…1f64`) and a notify characteristic (RX `…94b6`). On top of it runs **SDCP**, IQAir's
request/response protocol. The purifier never streams on its own, so the integration:

1. Connects, checks the link is encrypted (pairing if needed) and subscribes to RX.
2. Sends the SDCP *connect* frame, then reads the model name and fan-level table once.
3. Polls the dataPoints it needs with one `dpReadList` request per interval.
4. Sends commands as `dpWrite` frames on the same link, then polls immediately so the
   UI updates within about a second.

The connection is held open with automatic reconnects. Every step has a deadline
(connect + bond + handshake within 60 s, each request within 6 s), and a watchdog checks
once a minute that the session task is alive and has made progress recently; if not, it
restarts the task and logs a warning. After three failed connection attempts in a row the
integration logs a WARNING with the last error (and again every 30 attempts), so a
purifier that stays unavailable is explained in the log rather than silent.

**Diagnostics** (device page → ⋮ → *Download diagnostics*) include a `session` block:
the last connection attempt and error, which adapter/proxy the link used, how many
attempts have failed in a row, whether the session task is alive, and how many times the
watchdog had to restart it.

See
[docs/PROTOCOL.md](docs/PROTOCOL.md) for the frame format, CRC and dataPoint map, and
[tools/iqair_ble.py](tools/iqair_ble.py) for a standalone command-line client.

## Known limits

- Developed and tested on one HealthPro Plus XE. Other HealthPro models likely work, but
  their dataPoints may differ. Diagnostics output helps add them.
- Which of the first two filter-life values is the pre-filter and which is the gas
  filter isn't confirmed yet (both read 96% on the test unit).
- The unit's child lock isn't exposed: this model doesn't report a lock dataPoint.

## License

[MIT](LICENSE)
