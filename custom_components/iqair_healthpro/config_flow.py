"""Config flow for the IQAir HealthPro BLE integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from bleak.exc import BleakError

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from . import sdcp
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_SOURCE,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    SOURCE_AUTO,
)
from .coordinator import NotPairedError, async_probe, scanner_sources

_LOGGER = logging.getLogger(__name__)


IQAIR_MANUFACTURER_ID = 0x060A  # BLE company id in the purifier's advertisement


def _is_iqair(info: BluetoothServiceInfoBleak) -> bool:
    # Over the air the unit advertises a coded name (e.g. "NQQHN50NQONWTNFIN-P"), not
    # "IQAir ...", so match on IQAir's manufacturer id first.
    return IQAIR_MANUFACTURER_ID in info.manufacturer_data or (
        (info.name or "").lower().startswith("iqair")
    )


class IQAirConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, str] = {}
        self._probed_name = ""

    async def _probe(self, address: str) -> str | None:
        """Return an error key, or None if the purifier answered."""
        try:
            self._probed_name = await async_probe(self.hass, address)
        except NotPairedError:
            return "not_paired"
        except (sdcp.SdcpError, asyncio.TimeoutError, BleakError) as err:
            _LOGGER.debug("Probe of %s failed: %s", address, err)
            return "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error probing %s", address)
            return "unknown"
        return None

    def _title(self, address: str) -> str:
        if self._probed_name:
            return f"IQAir {self._probed_name}"
        return DEFAULT_NAME

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovered = {discovery_info.address: discovery_info.name}
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        address = self.unique_id
        assert address is not None
        name = self._discovered.get(address, DEFAULT_NAME)
        errors: dict[str, str] = {}
        if user_input is not None:
            if (err := await self._probe(address)) is None:
                return self.async_create_entry(title=self._title(address), data={})
            errors["base"] = err
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm", description_placeholders={"name": name}, errors=errors
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            if (err := await self._probe(address)) is None:
                return self.async_create_entry(title=self._title(address), data={})
            errors["base"] = err

        current = self._async_current_ids()
        for info in async_discovered_service_info(self.hass):
            if info.address not in current and _is_iqair(info):
                self._discovered[info.address] = info.name
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {a: f"{n} ({a})" for a, n in self._discovered.items()}
                    )
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return IQAirOptionsFlow()


class IQAirOptionsFlow(OptionsFlow):
    """Poll interval, and which adapter/proxy to connect through."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        current_source = opts.get(CONF_SOURCE, SOURCE_AUTO)
        sources = {SOURCE_AUTO: "Automatic (strongest signal)"}
        sources.update(scanner_sources(self.hass, self.config_entry.unique_id))
        sources.setdefault(current_source, current_source)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)),
                    vol.Required(CONF_SOURCE, default=current_source): vol.In(sources),
                }
            ),
        )
