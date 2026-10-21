"""Identify button: asks the purifier to signal itself."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IQAirConfigEntry, sdcp
from .entity import IQAirEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: IQAirConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([IQAirIdentify(entry.runtime_data)])


class IQAirIdentify(IQAirEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "identify")

    async def async_press(self) -> None:
        await self._write((sdcp.DP_IDENTIFY, 1))
