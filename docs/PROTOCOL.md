# SDCP over BLE — IQAir HealthPro

IQAir's *Smart Device Communication Protocol*, recovered from the IQAir AirVisual app
(7.4.4) and confirmed against a live HealthPro Plus XE. All integers are little-endian.

## Transport

| | UUID | Properties |
| --- | --- | --- |
| Service | `1b5ae7e4-f469-440f-a0b4-aed74acd94f8` | |
| TX (app → purifier) | `55340670-4e1c-471a-bd05-1891775a1f64` | write, write-without-response |
| RX (purifier → app) | `6f5e9f58-ed60-47a2-bbe4-ec93545b94b6` | read, notify (CCCD `0x2902`) |

- RX needs an **encrypted link**. Before pairing, reads fail with *Insufficient
  authentication/encryption*. After pairing, reading RX returns `01`.
- The purifier advertises a coded local name (e.g. `NQQHN50NQONWTNFIN-P`) and manufacturer
  data for company id `0x060a` = `05 00 08`. No service UUIDs are advertised.
- Requests are written in chunks of at most 20 bytes. Replies arrive as notifications,
  which may be split; reassemble by the frame length.

## Frames

```text
request : [cw][len u16][payload][crc16]
response: [cw|0x80][len u16][status][payload][crc16]
```

- `len` counts everything after the length field: payload + 2 for a request,
  1 + payload + 2 for a response. Total frame size = `len + 3`.
- `crc16` = **CRC-16/CCITT-FALSE** (poly `0x1021`, init `0xFFFF`, no reflection, no xor-out)
  over every preceding byte, stored little-endian. Check value: `crc("123456789") = 0x29B1`.
- The response control word is the request's with bit 7 set.

### Status byte

| Value | Meaning |
| --- | --- |
| `00` | ok |
| `80` | ok, more packets follow (multi-frame reply; concatenate payloads) |
| `01`–`0a` | error, busy, timeout, accessDenied, invalidRange, invalidSize, overflow, invalidCmd, invalidCrc, preconditionFailed |
| `fe` / `ff` | pending / unsolicited |

### Control words

| cw | Name | Request payload |
| --- | --- | --- |
| `01` | connect | `02 96 00 00` + u32 epoch seconds → reply `[protoVer u8][rxBufSize u16]` |
| `13` | dpReadList | concatenated u16 dataPoint ids → reply is TLV |
| `14` | dpWrite | `[id u16][value]` (no length) → empty reply |
| `15` | dpReadAll | empty → every dataPoint, as TLV over several frames |
| `16` | dpWriteList | repeat `[id u16][len u16][value]` |

Others seen in the app: `02` disconnect, `03` productInformation, `04` accessPermissions,
`10`/`11` dataPoint definitions, `12` dpRead, `20` restart, `21` fwUpdate,
`22` restoreDefaults, `31` virtualStorageWrite.

The purifier ignores everything until it receives `01` connect. TLV = repeat
`[id u16][len u16][value]`. Ids the unit doesn't have are silently left out of replies.

## Example (captured)

```text
>> 01 0a00 02960000 fe5ad86a fcdf            connect
<< 81 0700 00 02 1420 0124f6                 ok, protoVer 2, rxBuf 8212
>> 14 0500 9d13 00 2a5d                      dpWrite light (0x139d) = 0
<< 94 0300 00 ....                           ok
```

## DataPoints (HealthPro Plus XE)

| id | Name | Encoding | R/W |
| --- | --- | --- | --- |
| `0x0bcf` / `0x0bd0` / `0x0bd1` | PM2.5 / PM1 / PM10 | u16 µg/m³ | R |
| `0x0bc5` | fan rpm | u16 | R |
| `0x0bd6` | CADR | u32 m³/h | R |
| `0x07cf` | product errors | u32 bitfield, 0 = none | R |
| `0x03ea` | product name | ASCII | R |
| `0x1388` | fan speed | u8 % of max | R (write → accessDenied) |
| `0x138b` | **fan level** | u8 1–6 (needs Auto off) | **RW** |
| `0x13a5` | fan level table | 6 × u8 %, e.g. `22 37 52 62 71 98` | R |
| `0x139c` | standby | u8, 1 = off, 0 = running | **RW** |
| `0x139d` | display light | u8 bool | **RW** |
| `0x139e` | Auto mode | u8 bool | **RW** |
| `0x13a2` | identify | u8 bool | W |
| `0x1770` / `0x1771` | time on power / fan run | u32 seconds | R |
| `0x1772` | cumulative air volume | u32 m³ | R |
| `0x100e` | Wi-Fi RSSI | s8 dBm | R |
| `0xc73e` | filter life | 4 × u8 %: [pre?, gas?, HyperHEPA, unused] | R |
| `0x0fc3` / `0x0fc4` | timezone (s8 × 15 min) / DST flag | | R |
| `0x0fc5` | UTC sync time | u32 epoch | R |

`dpReadAll` returns about 97 dataPoints on this unit, including about 39 that the app has no
name for. `tools/iqair_ble.py --dump` prints them all.
