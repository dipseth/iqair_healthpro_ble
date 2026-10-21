"""Switches: Auto mode and the display light."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IQAirConfigEntry, sdcp
from .entity import IQAirEntity


@dataclass(frozen=True, kw_only=True)
class IQAirSwitchDescription(SwitchEntityDescription):
    dp_id: int


SWITCHES = (
    IQAirSwitchDescription(key="auto", translation_key="auto", dp_id=sdcp.DP_AUTO),
    IQAirSwitchDescription(
        key="light", translation_key="light", dp_id=sdcp.DP_LIGHT,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: IQAirConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(IQAirSwitch(entry.runtime_data, d) for d in SWITCHES)


class IQAirSwitch(IQAirEntity, SwitchEntity):
    entity_description: IQAirSwitchDescription

    def __init__(self, coordinator, description: IQAirSwitchDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._data_key = description.key

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get(self.entity_description.key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write((self.entity_description.dp_id, 1))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write((self.entity_description.dp_id, 0))
