"""Constants for the IQAir HealthPro BLE integration."""

from __future__ import annotations

DOMAIN = "iqair_healthpro"
DEFAULT_NAME = "IQAir HealthPro"
MANUFACTURER = "IQAir"

# One custom UART-style GATT service.
SERVICE_UUID = "1b5ae7e4-f469-440f-a0b4-aed74acd94f8"
TX_UUID = "55340670-4e1c-471a-bd05-1891775a1f64"  # app -> purifier (write)
RX_UUID = "6f5e9f58-ed60-47a2-bbe4-ec93545b94b6"  # purifier -> app (notify, needs bond)

CONF_SCAN_INTERVAL = "scan_interval"
CONF_SOURCE = "source"
SOURCE_AUTO = "auto"

DEFAULT_SCAN_INTERVAL = 30  # seconds between polls
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 600

REQUEST_TIMEOUT = 6.0  # seconds for one request: writes plus the reply frame
SESSION_SETUP_TIMEOUT = 60.0  # connect + bond check + handshake must finish in this
DISCONNECT_TIMEOUT = 10.0  # don't let a stuck disconnect hold up teardown
RECONNECT_DELAY = 20  # seconds between reconnect attempts
NOT_PAIRED_DELAY = 300  # back off harder when the host adapter has no bond
MAX_POLL_FAILURES = 3  # consecutive failed polls before reconnecting

# Watchdog: restart the session task if it exits or stops making progress.
WATCHDOG_INTERVAL = 60  # seconds between liveness checks
WATCHDOG_GRACE = 90  # slack on top of the longest legitimate quiet period
WARN_AFTER_FAILURES = 3  # consecutive failed sessions before logging a WARNING
WARN_EVERY_FAILURES = 30  # ...and again every N after that (~10 min at 20 s)
