"""Base entity for the IQAir HealthPro integration."""

from __future__ import annotations

import asyncio

from bleak.exc import BleakError

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from . import sdcp
from .const import DEFAULT_NAME, DOMAIN, MANUFACTURER
from .coordinator import IQAirCoordinator


class IQAirEntity(Entity):
    """Device info, push updates, availability, and error-wrapped writes."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Key in coordinator.data this entity needs; None = only needs a live link.
    _data_key: str | None = None

    def __init__(self, coordinator: IQAirCoordinator, key: str) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            identifiers={(DOMAIN, coordinator.address)},
            name=DEFAULT_NAME,
            manufacturer=MANUFACTURER,
            model=str(coordinator.data.get("product_name") or "HealthPro Plus"),
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        if not self.coordinator.connected:
            return False
        return self._data_key is None or self._data_key in self.coordinator.data

    async def _write(self, *writes: tuple[int, int]) -> None:
        try:
            await self.coordinator.async_write(*writes)
        except (sdcp.SdcpError, asyncio.TimeoutError, BleakError) as err:
            raise HomeAssistantError(f"IQAir command failed: {err}") from err
