"""SDCP — IQAir "Smart Device Communication Protocol" over BLE.

Pure protocol code (no Home Assistant imports) so it can be unit-tested on its own.
Recovered from the IQAir AirVisual app and confirmed against a live HealthPro Plus.

Frames (every int little-endian):
    request : [cw][len u16][payload][crc16]              len = len(payload) + 2
    response: [cw | 0x80][len u16][status][payload][crc16]
    crc16   = CRC-16/CCITT-FALSE over every byte before it.
Writes to the device are split into 20-byte chunks; replies arrive as notifications
that may also be split, so they are reassembled by length.

DataPoint payloads are TLV: repeat [id u16][len u16][value].
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# ---- control words ----------------------------------------------------------
CW_CONNECT = 0x01
CW_DP_READ_LIST = 0x13
CW_DP_WRITE = 0x14
CW_DP_READ_ALL = 0x15
RESPONSE_BIT = 0x80

# ---- status byte (responses) ------------------------------------------------
STATUS_OK = 0x00
STATUS_MORE = 0x80  # morePacketsFollow
STATUS_NAMES = {
    0x00: "ok",
    0x01: "error",
    0x02: "busy",
    0x03: "timeout",
    0x04: "accessDenied",
    0x05: "invalidRange",
    0x06: "invalidSize",
    0x07: "overflow",
    0x08: "invalidCmd",
    0x09: "invalidCrc",
    0x0A: "preconditionFailed",
    0x80: "morePacketsFollow",
    0xFE: "pending",
    0xFF: "unsolicited",
}

CHUNK = 20

# ---- dataPoint ids ----------------------------------------------------------
DP_PRODUCT_NAME = 0x03EA
DP_PRODUCT_ERRORS = 0x07CF
DP_PM25 = 0x0BCF
DP_PM1 = 0x0BD0
DP_PM10 = 0x0BD1
DP_FAN_RPM = 0x0BC5
DP_CADR = 0x0BD6
DP_FAN_PERCENT = 0x1388  # read-only (write -> accessDenied)
DP_FAN_LEVEL = 0x138B  # writable, 1..6
DP_STANDBY = 0x139C  # 1 = standby (off), 0 = running
DP_LIGHT = 0x139D
DP_AUTO = 0x139E
DP_IDENTIFY = 0x13A2
DP_FAN_LEVELS = 0x13A5  # percent for each of the 6 levels
DP_TIME_ON_POWER = 0x1770  # seconds
DP_TIME_FAN_RUN = 0x1771  # seconds
DP_AIR_VOLUME = 0x1772  # m3
DP_WIFI_RSSI = 0x100E  # s8 dBm
DP_FILTER_LIFE = 0xC73E  # 4 x u8 %: [pre?, gas?, main, unused]

# Polled every update cycle.
POLL_IDS = (
    DP_PM25, DP_PM1, DP_PM10, DP_FAN_RPM, DP_CADR, DP_PRODUCT_ERRORS,
    DP_FAN_PERCENT, DP_FAN_LEVEL, DP_STANDBY, DP_LIGHT, DP_AUTO,
    DP_TIME_ON_POWER, DP_TIME_FAN_RUN, DP_AIR_VOLUME, DP_WIFI_RSSI, DP_FILTER_LIFE,
)
# Read once per connection.
STATIC_IDS = (DP_PRODUCT_NAME, DP_FAN_LEVELS)

DEFAULT_FAN_LEVELS = (22, 37, 52, 62, 71, 98)


class SdcpError(Exception):
    """A request failed (no reply, bad CRC, or an error status)."""


# ---- CRC-16/CCITT-FALSE -----------------------------------------------------
def _make_table() -> list[int]:
    table = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else (c << 1)
        table.append(c & 0xFFFF)
    return table


_TABLE = _make_table()


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc = ((crc << 8) ^ _TABLE[((crc >> 8) ^ b) & 0xFF]) & 0xFFFF
    return crc


# ---- frames -----------------------------------------------------------------
def build_frame(cw: int, payload: bytes = b"") -> bytes:
    head = bytes([cw & 0xFF]) + (len(payload) + 2).to_bytes(2, "little") + payload
    return head + crc16(head).to_bytes(2, "little")


def chunks(frame: bytes, size: int = CHUNK) -> list[bytes]:
    return [frame[i : i + size] for i in range(0, len(frame), size)]


@dataclass(frozen=True)
class Frame:
    cw: int
    status: int | None
    payload: bytes
    crc_ok: bool

    @property
    def request_cw(self) -> int:
        return self.cw & ~RESPONSE_BIT


def parse_frame(raw: bytes) -> Frame:
    cw = raw[0]
    length = int.from_bytes(raw[1:3], "little")
    crc_ok = crc16(raw[:-2]) == int.from_bytes(raw[-2:], "little")
    if cw & RESPONSE_BIT and length >= 3:
        return Frame(cw, raw[3], bytes(raw[4:-2]), crc_ok)
    return Frame(cw, None, bytes(raw[3:-2]), crc_ok)


class Reassembler:
    """Join notification chunks into whole frames (len + 3 bytes each)."""

    def __init__(self) -> None:
        self._buf = b""

    def reset(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out = []
        while len(self._buf) >= 3:
            total = int.from_bytes(self._buf[1:3], "little") + 3
            if total < 5 or total > 16384:  # garbage; resync on the next notification
                self._buf = b""
                break
            if len(self._buf) < total:
                break
            out.append(self._buf[:total])
            self._buf = self._buf[total:]
        return out


# ---- payloads ---------------------------------------------------------------
def connect_payload(epoch: int | None = None) -> bytes:
    epoch = int(time.time()) if epoch is None else epoch
    return bytes([0x02, 0x96, 0x00, 0x00]) + (epoch & 0xFFFFFFFF).to_bytes(4, "little")


def read_list_payload(ids) -> bytes:
    return b"".join(i.to_bytes(2, "little") for i in ids)


def write_payload(dp_id: int, value: bytes) -> bytes:
    return dp_id.to_bytes(2, "little") + value


def decode_tlv(payload: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    i = 0
    while i + 4 <= len(payload):
        dp_id = int.from_bytes(payload[i : i + 2], "little")
        n = int.from_bytes(payload[i + 2 : i + 4], "little")
        if i + 4 + n > len(payload):
            raise SdcpError(f"truncated TLV at offset {i}")
        out[dp_id] = payload[i + 4 : i + 4 + n]
        i += 4 + n
    return out


# ---- value decoding ---------------------------------------------------------
def _u(v: bytes) -> int:
    return int.from_bytes(v, "little")


def _s(v: bytes) -> int:
    return int.from_bytes(v, "little", signed=True)


def decode_state(raw: dict[int, bytes]) -> dict[str, object]:
    """Turn raw dataPoints into the named values entities read."""
    s: dict[str, object] = {}

    def put(key, dp_id, fn):
        v = raw.get(dp_id)
        if v:
            s[key] = fn(v)

    put("pm25", DP_PM25, _u)
    put("pm1", DP_PM1, _u)
    put("pm10", DP_PM10, _u)
    put("fan_rpm", DP_FAN_RPM, _u)
    put("cadr", DP_CADR, _u)
    put("fan_percent", DP_FAN_PERCENT, _u)
    put("fan_level", DP_FAN_LEVEL, _u)
    put("power", DP_STANDBY, lambda v: _u(v) == 0)
    put("light", DP_LIGHT, lambda v: _u(v) != 0)
    put("auto", DP_AUTO, lambda v: _u(v) != 0)
    put("time_on_power", DP_TIME_ON_POWER, _u)
    put("time_fan_run", DP_TIME_FAN_RUN, _u)
    put("air_volume", DP_AIR_VOLUME, _u)
    put("wifi_rssi", DP_WIFI_RSSI, _s)
    put("errors", DP_PRODUCT_ERRORS, _u)
    put("product_name", DP_PRODUCT_NAME, lambda v: v.rstrip(b"\x00").decode(errors="replace"))
    put("fan_levels", DP_FAN_LEVELS, tuple)
    f = raw.get(DP_FILTER_LIFE)
    if f and len(f) >= 3:
        s["filter_pre"], s["filter_gas"], s["filter_main"] = f[0], f[1], f[2]
    return s
