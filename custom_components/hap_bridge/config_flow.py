"""Config and options flow for HAP Bridge."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import (
    CONF_EXCLUDE_DEVICES,
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_INTEGRATIONS,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DOMAIN,
)


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

        entities = [
            e for e in er.async_get(self.hass).entities.values() if e.platform != DOMAIN
        ]
        options = self.config_entry.options

        def multi_select(choices: set[str]) -> selector.SelectSelector:
            return selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=sorted(choices),
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_INCLUDE_INTEGRATIONS,
                        default=options.get(CONF_INCLUDE_INTEGRATIONS, []),
                    ): multi_select({e.platform for e in entities}),
                    vol.Optional(
                        CONF_INCLUDE_DOMAINS,
                        default=options.get(CONF_INCLUDE_DOMAINS, []),
                    ): multi_select({e.domain for e in entities}),
                    vol.Optional(
                        CONF_EXCLUDE_DEVICES,
                        default=options.get(CONF_EXCLUDE_DEVICES, []),
                    ): selector.DeviceSelector(selector.DeviceSelectorConfig(multiple=True)),
                    vol.Optional(
                        CONF_EXCLUDE_ENTITIES,
                        default=options.get(CONF_EXCLUDE_ENTITIES, []),
                    ): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
                }
            ),
        )
