"""The IQAir HealthPro BLE integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_SCAN_INTERVAL, CONF_SOURCE, DEFAULT_SCAN_INTERVAL, SOURCE_AUTO
from .coordinator import IQAirCoordinator, pick_device

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.FAN,
    Platform.SENSOR,
    Platform.SWITCH,
]

type IQAirConfigEntry = ConfigEntry[IQAirCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: IQAirConfigEntry) -> bool:
    address = entry.unique_id
    assert address is not None
    source = entry.options.get(CONF_SOURCE, SOURCE_AUTO)
    if pick_device(hass, address, source) is None:
        raise ConfigEntryNotReady(f"IQAir {address} not in Bluetooth range")

    coordinator = IQAirCoordinator(
        hass,
        address,
        entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        source,
        entry_id=entry.entry_id,
    )
    await coordinator.async_start()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: IQAirConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok


async def _async_reload(hass: HomeAssistant, entry: IQAirConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
