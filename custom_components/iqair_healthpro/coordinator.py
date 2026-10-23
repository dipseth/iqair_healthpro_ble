"""Persistent BLE session to an IQAir HealthPro purifier.

The purifier speaks SDCP (see sdcp.py): it only answers requests and never streams on
its own, so this coordinator holds one connection open, polls the dataPoints entities
need, and sends writes over the same link. Reads of the RX characteristic need an
encrypted (bonded) link; the bond lives in the Bluetooth adapter, not in Home Assistant.

The session runs in one background task. Every step of it is bounded by a timeout, and
a watchdog restarts the task if it ever exits or goes quiet, so a stuck Bluetooth stack
can't leave the purifier unavailable until the next reload.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from . import sdcp
from .const import (
    DISCONNECT_TIMEOUT,
    DOMAIN,
    MAX_POLL_FAILURES,
    NOT_PAIRED_DELAY,
    RECONNECT_DELAY,
    REQUEST_TIMEOUT,
    RX_UUID,
    SESSION_SETUP_TIMEOUT,
    SOURCE_AUTO,
    TX_UUID,
    WARN_AFTER_FAILURES,
    WARN_EVERY_FAILURES,
    WATCHDOG_GRACE,
    WATCHDOG_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class NotPairedError(Exception):
    """The adapter has no bond with the purifier, and pairing from HA failed."""

    def __init__(self, message: str, retry_delay: float = NOT_PAIRED_DELAY) -> None:
        super().__init__(message)
        self.retry_delay = retry_delay


def pick_device(
    hass: HomeAssistant, address: str, source: str = SOURCE_AUTO
) -> BLEDevice | None:
    """Best connectable path to the purifier, optionally pinned to one adapter/proxy.

    Bonds are per adapter, so a purifier paired with the host adapter can't be read
    through a proxy that has no bond. Pinning the source avoids that mismatch.
    """
    if source in (None, "", SOURCE_AUTO):
        return bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    for dev in bluetooth.async_scanner_devices_by_address(hass, address, connectable=True):
        if dev.scanner.source == source:
            return dev.ble_device
    return None


def scanner_sources(hass: HomeAssistant, address: str) -> dict[str, str]:
    """{source: label} for every connectable adapter/proxy that currently hears it."""
    out = {}
    for dev in bluetooth.async_scanner_devices_by_address(hass, address, connectable=True):
        out[dev.scanner.source] = f"{dev.scanner.name} ({dev.advertisement.rssi} dBm)"
    return out


def _is_proxy_client(client: BleakClient) -> bool:
    """True when the live connection goes through an ESPHome Bluetooth proxy."""
    backend = getattr(client, "_backend", None)
    return type(backend).__module__.startswith("bleak_esphome")


def _client_path(client: BleakClient) -> str:
    """Human-readable adapter/proxy the connection ended up on."""
    backend = getattr(client, "_backend", None)
    module = type(backend).__module__
    if module.startswith("bleak_esphome"):
        info = getattr(backend, "_device_info", None)
        return f"proxy {getattr(info, 'name', None) or '?'}"
    if "bluez" in module:
        return "host adapter (BlueZ)"
    return type(backend).__name__


class IQAirCoordinator:
    """Owns the BLE session: connect, bond check, handshake, poll, write."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        scan_interval: int,
        source: str = SOURCE_AUTO,
        entry_id: str | None = None,
    ) -> None:
        self.hass = hass
        self.address = address
        self.scan_interval = scan_interval
        self.source = source
        self.entry_id = entry_id
        self.raw: dict[int, bytes] = {}
        self.data: dict[str, object] = {}
        self.connected = False
        self.last_update: float | None = None
        # Diagnostics: what the session loop has been doing lately.
        self.last_attempt: float | None = None
        self.last_error: str | None = None
        self.last_path: str | None = None
        self.consecutive_failures = 0
        self.watchdog_restarts = 0
        self._last_activity = time.monotonic()
        self._client: BleakClient | None = None
        self._listeners: list[Callable[[], None]] = []
        self._run_task: asyncio.Task | None = None
        self._unsub_watchdog: Callable[[], None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._rx = sdcp.Reassembler()
        self._pending_cw: int | None = None
        self._pending: list[sdcp.Frame] = []
        self._pending_fut: asyncio.Future | None = None
        self._warned_not_paired = False

    # ---- listeners ---------------------------------------------------------
    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update_callback)

        def _remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove

    @callback
    def _notify_listeners(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001 - one bad entity must not kill the session
                _LOGGER.exception("%s: error in update listener", self.address)

    @callback
    def _set_connected(self, value: bool) -> None:
        if self.connected != value:
            self.connected = value
            self._notify_listeners()

    @property
    def fan_levels(self) -> tuple[int, ...]:
        levels = self.data.get("fan_levels")
        return levels if levels and len(levels) == 6 else sdcp.DEFAULT_FAN_LEVELS

    # ---- lifecycle ---------------------------------------------------------
    async def async_start(self) -> None:
        self._stop.clear()
        self._start_run_task()
        self._unsub_watchdog = async_track_time_interval(
            self.hass, self._async_watchdog, timedelta(seconds=WATCHDOG_INTERVAL)
        )

    async def async_stop(self) -> None:
        if self._unsub_watchdog is not None:
            self._unsub_watchdog()
            self._unsub_watchdog = None
        self._stop.set()
        self._wake.set()
        await self._async_cancel_run_task()
        client, self._client = self._client, None
        if client is not None:
            await self._disconnect(client)

    @callback
    def _start_run_task(self) -> None:
        self._touch()
        self._run_task = self.hass.async_create_background_task(
            self._run(), f"iqair_healthpro {self.address}"
        )

    async def _async_cancel_run_task(self) -> None:
        task, self._run_task = self._run_task, None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.wait({task}, timeout=DISCONNECT_TIMEOUT)

    @callback
    def _touch(self) -> None:
        """Mark the session loop as alive (for the watchdog)."""
        self._last_activity = time.monotonic()

    @property
    def stall_limit(self) -> float:
        """Longest the loop may legitimately go without touching `_last_activity`."""
        return max(NOT_PAIRED_DELAY, self.scan_interval) + SESSION_SETUP_TIMEOUT + WATCHDOG_GRACE

    @property
    def seconds_since_activity(self) -> float:
        return time.monotonic() - self._last_activity

    @property
    def session_task_alive(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    async def _async_watchdog(self, _now) -> None:
        """Restart the session task if it died or stopped making progress."""
        if self._stop.is_set():
            return
        task = self._run_task
        if task is None or task.done():
            reason = "session task exited"
            if task is not None and not task.cancelled() and task.exception() is not None:
                reason = f"session task crashed: {task.exception()!r}"
        elif (age := self.seconds_since_activity) > self.stall_limit:
            reason = f"no session activity for {age:.0f}s"
        else:
            return
        self.watchdog_restarts += 1
        self.last_error = f"watchdog restart: {reason}"
        _LOGGER.warning("%s: %s; restarting the Bluetooth session", self.address, reason)
        await self._async_cancel_run_task()
        client, self._client = self._client, None
        if client is not None:
            await self._disconnect(client)
        self._set_connected(False)
        self._start_run_task()

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake early on stop / disconnect / refresh request."""
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._touch()
                delay = RECONNECT_DELAY
                try:
                    reason = await self._session()
                except asyncio.CancelledError:
                    raise
                except NotPairedError as err:
                    delay = err.retry_delay
                    level = logging.DEBUG if self._warned_not_paired else logging.ERROR
                    self._warned_not_paired = True
                    _LOGGER.log(level, "%s: %s", self.address, err)
                    self._record_failure(str(err), quiet=True)
                except Exception as err:  # noqa: BLE001 - keep the loop alive
                    self._record_failure(f"{type(err).__name__}: {err}")
                else:
                    if reason:
                        self._record_failure(reason)
                self._set_connected(False)
                if not self._stop.is_set():
                    self._touch()
                    await self._sleep(delay)
        finally:
            self._set_connected(False)

    @callback
    def _record_failure(self, message: str, quiet: bool = False) -> None:
        """Remember why the session ended; nag at WARNING if it keeps failing."""
        self.last_error = message
        self.consecutive_failures += 1
        n = self.consecutive_failures
        if n == WARN_AFTER_FAILURES or (
            n > WARN_AFTER_FAILURES and n % WARN_EVERY_FAILURES == 0
        ):
            _LOGGER.warning(
                "%s: still not connected after %d attempts (last error: %s)",
                self.address, n, message,
            )
        elif not quiet:
            _LOGGER.debug("%s: session ended (%s); retrying", self.address, message)

    # ---- connection --------------------------------------------------------
    async def _connect(self, device: BLEDevice) -> BleakClient:
        _LOGGER.debug("%s: connecting via %s", self.address, getattr(device, "details", ""))
        return await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.address,
            disconnected_callback=lambda _c: self._wake.set(),
            max_attempts=3,
        )

    async def _disconnect(self, client: BleakClient) -> None:
        try:
            async with asyncio.timeout(DISCONNECT_TIMEOUT):
                await client.disconnect()
        except Exception as err:  # noqa: BLE001 - best effort on teardown
            _LOGGER.debug("%s: disconnect failed (%s)", self.address, err)

    async def open_session(self, client: BleakClient) -> None:
        """Bond check, subscribe, SDCP handshake, one-off static reads."""
        await self._ensure_bonded(client)
        self._rx.reset()
        self._client = client
        await client.start_notify(RX_UUID, self._on_rx)
        reply = await self._request(sdcp.CW_CONNECT, sdcp.connect_payload())
        if len(reply) >= 3:
            _LOGGER.debug(
                "%s: SDCP connected, protoVer=%s rxBuf=%s",
                self.address, reply[0], int.from_bytes(reply[1:3], "little"),
            )
        static = await self._request(
            sdcp.CW_DP_READ_LIST, sdcp.read_list_payload(sdcp.STATIC_IDS)
        )
        self._merge(sdcp.decode_tlv(static))

    async def _ensure_bonded(self, client: BleakClient) -> None:
        try:
            await client.read_gatt_char(RX_UUID)
            return
        except BleakError as err:
            _LOGGER.debug("%s: protected read failed (%s); trying to pair", self.address, err)
        try:
            await client.pair()
            await client.read_gatt_char(RX_UUID)
        except (BleakError, NotImplementedError, asyncio.TimeoutError) as err:
            if _is_proxy_client(client):
                # Proxies pair on their own; a failure here is usually transient
                # (busy proxy, dropped link), so retry at the normal cadence.
                raise NotPairedError(
                    f"pairing through {_client_path(client)} failed ({err}); "
                    f"retrying in {RECONNECT_DELAY}s",
                    retry_delay=RECONNECT_DELAY,
                ) from err
            raise NotPairedError(
                "the Bluetooth adapter is not bonded with the purifier and pairing "
                f"from Home Assistant failed ({err}). Pair once with the 6-digit code "
                "from the unit (see the integration README), then reload."
            ) from err

    async def _session(self) -> str | None:
        """One connection lifetime. Returns why it ended, or None on stop."""
        self.last_attempt = time.time()
        device = pick_device(self.hass, self.address, self.source)
        if device is None:
            self.last_path = None
            return f"not in range (source={self.source})"
        client: BleakClient | None = None
        try:
            async with asyncio.timeout(SESSION_SETUP_TIMEOUT):
                client = await self._connect(device)
                self.last_path = _client_path(client)
                await self.open_session(client)
            self._warned_not_paired = False
            if self.consecutive_failures:
                _LOGGER.info(
                    "%s: connected via %s after %d failed attempts",
                    self.address, self.last_path, self.consecutive_failures,
                )
            self.consecutive_failures = 0
            self.last_error = None
            self._update_device_model()
            failures = 0
            while client.is_connected and not self._stop.is_set():
                self._touch()
                try:
                    await self.async_poll()
                    failures = 0
                    self._set_connected(True)
                except (sdcp.SdcpError, asyncio.TimeoutError, BleakError) as err:
                    failures += 1
                    _LOGGER.debug("%s: poll failed (%s), %d/%d",
                                  self.address, err, failures, MAX_POLL_FAILURES)
                    if failures >= MAX_POLL_FAILURES:
                        raise
                await self._sleep(self.scan_interval)
            return None if self._stop.is_set() else "link dropped"
        finally:
            self._client = None
            if client is not None:
                await self._disconnect(client)

    @callback
    def _update_device_model(self) -> None:
        """Entities are created before the first connect, so fill in the real model now."""
        model = self.data.get("product_name")
        if not model:
            return
        reg = dr.async_get(self.hass)
        lookup = getattr(reg, "async_get_device_by_identifier", None)
        if self.entry_id and lookup is not None:
            device = lookup((DOMAIN, self.address), self.entry_id)
        else:  # Home Assistant < 2026.x
            device = reg.async_get_device(identifiers={(DOMAIN, self.address)})
        if device is not None and device.model != model:
            reg.async_update_device(device.id, model=str(model))

    # ---- requests ----------------------------------------------------------
    async def _request(self, cw: int, payload: bytes = b"") -> bytes:
        async with self._lock:
            client = self._client
            if client is None or not client.is_connected:
                raise sdcp.SdcpError("not connected")
            loop = asyncio.get_running_loop()
            self._pending_cw = cw | sdcp.RESPONSE_BIT
            self._pending = []
            self._pending_fut = fut = loop.create_future()
            try:
                # One deadline for the writes and the reply, so a write that never
                # completes can't hold the lock (and the session) forever.
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    for part in sdcp.chunks(sdcp.build_frame(cw, payload)):
                        await client.write_gatt_char(TX_UUID, part, response=True)
                    frames: list[sdcp.Frame] = await fut
            finally:
                self._pending_cw = None
                self._pending_fut = None
        status = frames[-1].status
        if status != sdcp.STATUS_OK:
            raise sdcp.SdcpError(
                f"cw 0x{cw:02x} -> {sdcp.STATUS_NAMES.get(status, status)}"
            )
        return b"".join(f.payload for f in frames)

    @callback
    def _on_rx(self, _char, data: bytearray) -> None:
        for raw in self._rx.feed(bytes(data)):
            frame = sdcp.parse_frame(raw)
            fut = self._pending_fut
            if not frame.crc_ok:
                _LOGGER.debug("%s: bad CRC on %s", self.address, raw.hex())
                if fut is not None and not fut.done():
                    fut.set_exception(sdcp.SdcpError("bad CRC"))
                continue
            if fut is not None and not fut.done() and frame.cw == self._pending_cw:
                self._pending.append(frame)
                if frame.status != sdcp.STATUS_MORE:
                    fut.set_result(self._pending)
                continue
            # Anything else is unsolicited; keep dataPoint pushes if they parse.
            _LOGGER.debug("%s: unsolicited frame %s", self.address, raw.hex())
            try:
                tlv = sdcp.decode_tlv(frame.payload)
            except sdcp.SdcpError:
                continue
            if tlv:
                self._merge(tlv)
                self._notify_listeners()

    def _merge(self, tlv: dict[int, bytes]) -> None:
        self.raw.update(tlv)
        self.data = sdcp.decode_state(self.raw)

    async def async_poll(self) -> None:
        body = await self._request(sdcp.CW_DP_READ_LIST, sdcp.read_list_payload(sdcp.POLL_IDS))
        self._merge(sdcp.decode_tlv(body))
        self.last_update = time.time()
        self._notify_listeners()

    async def async_write(self, *writes: tuple[int, int]) -> None:
        """Write u8 values in order [(dataPoint, value), ...], then refresh state."""
        try:
            for dp_id, value in writes:
                await self._request(sdcp.CW_DP_WRITE, sdcp.write_payload(dp_id, bytes([value])))
                await asyncio.sleep(0.2)
        finally:
            try:
                await self.async_poll()
            except (sdcp.SdcpError, asyncio.TimeoutError, BleakError) as err:
                _LOGGER.debug("%s: refresh after write failed (%s)", self.address, err)


async def async_probe(hass: HomeAssistant, address: str, source: str = SOURCE_AUTO) -> str:
    """Connect once, bond-check, handshake; return the product name. For the config flow."""
    coord = IQAirCoordinator(hass, address, 0, source)
    device = pick_device(hass, address, source)
    if device is None:
        raise sdcp.SdcpError("purifier not in range")
    async with asyncio.timeout(SESSION_SETUP_TIMEOUT):
        client = await coord._connect(device)
        try:
            await coord.open_session(client)
        finally:
            coord._client = None
            await coord._disconnect(client)
    return str(coord.data.get("product_name") or "")
