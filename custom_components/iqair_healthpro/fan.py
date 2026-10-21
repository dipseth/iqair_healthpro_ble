"""Fan entity: power, 6 speed levels, and the purifier's Auto mode as a preset."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from . import IQAirConfigEntry, sdcp
from .entity import IQAirEntity

SPEED_RANGE = (1, 6)
PRESET_AUTO = "auto"


async def async_setup_entry(
    hass: HomeAssistant, entry: IQAirConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([IQAirFan(entry.runtime_data)])


class IQAirFan(IQAirEntity, FanEntity):
    _attr_name = None  # the device's main entity
    _attr_translation_key = "purifier"
    _attr_preset_modes = [PRESET_AUTO]
    _attr_speed_count = 6
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _data_key = "power"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "fan")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("power")

    @property
    def percentage(self) -> int | None:
        if not self.is_on:
            return 0
        level = self.coordinator.data.get("fan_level")
        return ranged_value_to_percentage(SPEED_RANGE, level) if level else None

    @property
    def preset_mode(self) -> str | None:
        return PRESET_AUTO if self.coordinator.data.get("auto") else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        return {"fan_level": d.get("fan_level"), "motor_speed_percent": d.get("fan_percent")}

    def _power_on(self) -> list[tuple[int, int]]:
        return [] if self.is_on else [(sdcp.DP_STANDBY, 0)]

    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any
    ) -> None:
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        elif percentage is not None:
            await self.async_set_percentage(percentage)
        else:
            await self._write((sdcp.DP_STANDBY, 0))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write((sdcp.DP_STANDBY, 1))

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return
        # Round to the nearest level: the UI's 6-step slider sends 17/33/50/67/83/100.
        level = max(1, min(6, round(percentage_to_ranged_value(SPEED_RANGE, percentage))))
        # A manual level only sticks with Auto off.
        await self._write(*self._power_on(), (sdcp.DP_AUTO, 0), (sdcp.DP_FAN_LEVEL, level))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self._write(*self._power_on(), (sdcp.DP_AUTO, 1))
