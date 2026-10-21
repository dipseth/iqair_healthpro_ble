"""Persistent BLE session to an IQAir HealthPro purifier.

The purifier speaks SDCP (see sdcp.py): it only answers requests and never streams on
its own, so this coordinator holds one connection open, polls the dataPoints entities
need, and sends writes over the same link. Reads of the RX characteristic need an
encrypted (bonded) link; the bond lives in the Bluetooth adapter, not in Home Assistant.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from . import sdcp
from .const import (
    DOMAIN,
    MAX_POLL_FAILURES,
    NOT_PAIRED_DELAY,
    RECONNECT_DELAY,
    REQUEST_TIMEOUT,
    RX_UUID,
    SOURCE_AUTO,
    TX_UUID,
)

_LOGGER = logging.getLogger(__name__)


class NotPairedError(Exception):
    """The adapter has no bond with the purifier, and pairing from HA failed."""


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


class IQAirCoordinator:
    """Owns the BLE session: connect, bond check, handshake, poll, write."""

    def __init__(
        self, hass: HomeAssistant, address: str, scan_interval: int, source: str = SOURCE_AUTO
    ) -> None:
        self.hass = hass
        self.address = address
        self.scan_interval = scan_interval
        self.source = source
        self.raw: dict[int, bytes] = {}
        self.data: dict[str, object] = {}
        self.connected = False
        self.last_update: float | None = None
        self._client: BleakClient | None = None
        self._listeners: list[Callable[[], None]] = []
        self._run_task: asyncio.Task | None = None
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
            cb()

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
        self._run_task = self.hass.async_create_background_task(
            self._run(), f"iqair_healthpro {self.address}"
        )

    async def async_stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 - best effort on teardown
                pass
        if self._run_task is not None:
            self._run_task.cancel()

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake early on stop / disconnect / refresh request."""
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            delay = RECONNECT_DELAY
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except NotPairedError as err:
                delay = NOT_PAIRED_DELAY
                level = logging.DEBUG if self._warned_not_paired else logging.ERROR
                self._warned_not_paired = True
                _LOGGER.log(level, "%s: %s", self.address, err)
            except Exception as err:  # noqa: BLE001 - keep the loop alive
                _LOGGER.debug("%s: session ended (%s); retrying", self.address, err)
            self._set_connected(False)
            if not self._stop.is_set():
                await self._sleep(delay)

    # ---- connection --------------------------------------------------------
    async def _connect(self) -> BleakClient | None:
        device = pick_device(self.hass, self.address, self.source)
        if device is None:
            _LOGGER.debug("%s: not in range (source=%s)", self.address, self.source)
            return None
        _LOGGER.debug("%s: connecting via %s", self.address, getattr(device, "details", ""))
        return await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.address,
            disconnected_callback=lambda _c: self._wake.set(),
            max_attempts=3,
        )

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
            raise NotPairedError(
                "the Bluetooth adapter is not bonded with the purifier and pairing "
                f"from Home Assistant failed ({err}). Pair once with the 6-digit code "
                "from the unit (see the integration README), then reload."
            ) from err

    async def _session(self) -> None:
        client = await self._connect()
        if client is None:
            return
        try:
            await self.open_session(client)
            self._warned_not_paired = False
            self._update_device_model()
            failures = 0
            while client.is_connected and not self._stop.is_set():
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
        finally:
            self._client = None
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    @callback
    def _update_device_model(self) -> None:
        """Entities are created before the first connect, so fill in the real model now."""
        model = self.data.get("product_name")
        if not model:
            return
        reg = dr.async_get(self.hass)
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
                for part in sdcp.chunks(sdcp.build_frame(cw, payload)):
                    await client.write_gatt_char(TX_UUID, part, response=True)
                frames: list[sdcp.Frame] = await asyncio.wait_for(fut, REQUEST_TIMEOUT)
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
    client = await coord._connect()
    if client is None:
        raise sdcp.SdcpError("purifier not in range")
    try:
        await coord.open_session(client)
    finally:
        coord._client = None
        await client.disconnect()
    return str(coord.data.get("product_name") or "")
