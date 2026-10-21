"""Sensor entities: particulates, fan, filter life, usage counters."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IQAirConfigEntry
from .entity import IQAirEntity


@dataclass(frozen=True, kw_only=True)
class IQAirSensorDescription(SensorEntityDescription):
    scale: float = 1.0  # raw value * scale = native value


_PM = dict(
    native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    state_class=SensorStateClass.MEASUREMENT,
)
_FILTER = dict(native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT)
_HOURS = dict(
    device_class=SensorDeviceClass.DURATION,
    native_unit_of_measurement=UnitOfTime.HOURS,
    state_class=SensorStateClass.TOTAL_INCREASING,
    entity_category=EntityCategory.DIAGNOSTIC,
    suggested_display_precision=0,
    scale=1 / 3600,  # device counts seconds
)

SENSORS: tuple[IQAirSensorDescription, ...] = (
    IQAirSensorDescription(key="pm25", device_class=SensorDeviceClass.PM25, **_PM),
    IQAirSensorDescription(key="pm1", device_class=SensorDeviceClass.PM1, **_PM),
    IQAirSensorDescription(key="pm10", device_class=SensorDeviceClass.PM10, **_PM),
    IQAirSensorDescription(
        key="fan_percent", translation_key="fan_percent",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
    ),
    IQAirSensorDescription(
        key="fan_rpm", translation_key="fan_rpm",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    IQAirSensorDescription(
        key="cadr", translation_key="cadr",
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        native_unit_of_measurement=UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    IQAirSensorDescription(key="filter_main", translation_key="filter_main", **_FILTER),
    IQAirSensorDescription(key="filter_pre", translation_key="filter_pre", **_FILTER),
    IQAirSensorDescription(key="filter_gas", translation_key="filter_gas", **_FILTER),
    IQAirSensorDescription(
        key="air_volume", translation_key="air_volume",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
    ),
    IQAirSensorDescription(key="time_on_power", translation_key="time_on_power", **_HOURS),
    IQAirSensorDescription(key="time_fan_run", translation_key="time_fan_run", **_HOURS),
    IQAirSensorDescription(
        key="wifi_rssi", translation_key="wifi_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: IQAirConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(IQAirSensor(entry.runtime_data, d) for d in SENSORS)


class IQAirSensor(IQAirEntity, SensorEntity):
    entity_description: IQAirSensorDescription

    def __init__(self, coordinator, description: IQAirSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._data_key = description.key

    @property
    def native_value(self) -> float | int | None:
        value = self.coordinator.data.get(self.entity_description.key)
        if value is None:
            return None
        scale = self.entity_description.scale
        return value if scale == 1.0 else value * scale
