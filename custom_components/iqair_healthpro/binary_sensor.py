"""Binary sensors: BLE link state and the purifier's error flag."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IQAirConfigEntry
from .entity import IQAirEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: IQAirConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    c = entry.runtime_data
    async_add_entities([IQAirConnected(c), IQAirProblem(c)])


class IQAirConnected(IQAirEntity, BinarySensorEntity):
    """On while the bonded SDCP session is up and polls succeed."""

    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "connected")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.connected


class IQAirProblem(IQAirEntity, BinarySensorEntity):
    """On when the purifier reports a non-zero product error code."""

    _attr_translation_key = "problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _data_key = "errors"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "problem")

    @property
    def is_on(self) -> bool | None:
        errors = self.coordinator.data.get("errors")
        return None if errors is None else errors != 0

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        errors = self.coordinator.data.get("errors")
        return {"error_code": None if errors is None else f"0x{errors:08x}"}
