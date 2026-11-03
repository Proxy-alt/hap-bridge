"""Config and options flow for HAP Bridge."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import CONF_INCLUDE_INTEGRATIONS, DEFAULT_NAME, DEFAULT_PORT, DOMAIN


class HapBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._async_abort_entries_match({CONF_PORT: user_input[CONF_PORT]})
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        used = {e.data.get(CONF_PORT) for e in self._async_current_entries()}
        port = DEFAULT_PORT
        while port in used:
            port += 1
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                    vol.Required(CONF_PORT, default=port): vol.All(
                        vol.Coerce(int), vol.Range(min=1024, max=65535)
                    ),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HapBridgeOptionsFlow()


class HapBridgeOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        domains = sorted({e.platform for e in er.async_get(self.hass).entities.values()})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_INCLUDE_INTEGRATIONS,
                        default=self.config_entry.options.get(CONF_INCLUDE_INTEGRATIONS, []),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=domains,
                            multiple=True,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )
