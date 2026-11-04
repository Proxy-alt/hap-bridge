"""Config and options flow for HAP Bridge."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import (
    CONF_ALARM_CODES,
    CONF_CAMERA_AUDIO,
    CONF_EXCLUDE_DEVICES,
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_INTEGRATIONS,
    CONF_NAMES,
    CONF_NO_PREBUFFER,
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


# HomeKit names: letters, digits, spaces and a little punctuation, starting
# and ending with a letter or digit. The Home app rejects anything else.
_VALID_NAME = re.compile(r"^[\w](?:[\w '.,&-]*[\w])?$")


class HapBridgeOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._key: str | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["filters", "rename", "camera_audio", "alarm_codes"])

    def _save(self, **changes: Any) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    async def async_step_filters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(**user_input)

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
            step_id="filters",
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

    def _accessories(self) -> dict[str, tuple[str, str]]:
        """{key: (name now, name the profile gave)} for the running bridge."""
        bridge = getattr(self.config_entry, "runtime_data", None)
        if bridge is None:
            return {}
        return {
            spec.key: (spec.name, spec.default_name) for spec, _acc in bridge.accessories()
        }

    async def async_step_rename(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        accessories = self._accessories()
        if not accessories:
            return self.async_abort(reason="not_running")
        if user_input is not None:
            self._key = user_input["accessory"]
            return await self.async_step_rename_accessory()
        names = self.config_entry.options.get(CONF_NAMES) or {}
        choices = [
            selector.SelectOptionDict(
                value=key,
                label=f"{now} (was {default})" if key in names else now,
            )
            for key, (now, default) in sorted(accessories.items(), key=lambda kv: kv[1][0].lower())
        ]
        return self.async_show_form(
            step_id="rename",
            data_schema=vol.Schema({
                vol.Required("accessory"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=choices, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                ),
            }),
        )

    async def async_step_rename_accessory(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._key is not None
        now, default = self._accessories().get(self._key, ("", ""))
        names = dict(self.config_entry.options.get(CONF_NAMES) or {})
        errors: dict[str, str] = {}
        if user_input is not None:
            name = " ".join(user_input.get(CONF_NAME, "").split())
            if not name or name == default:
                names.pop(self._key, None)
                return self._save(**{CONF_NAMES: names})
            if _VALID_NAME.match(name):
                names[self._key] = name
                return self._save(**{CONF_NAMES: names})
            errors[CONF_NAME] = "invalid_name"
        return self.async_show_form(
            step_id="rename_accessory",
            data_schema=vol.Schema({vol.Optional(CONF_NAME, default=now): str}),
            description_placeholders={"default": default},
            errors=errors,
        )

    async def async_step_camera_audio(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(**{
                CONF_CAMERA_AUDIO: user_input.get(CONF_CAMERA_AUDIO, []),
                CONF_NO_PREBUFFER: user_input.get(CONF_NO_PREBUFFER, []),
            })
        options = self.config_entry.options
        cameras = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="camera", multiple=True)
        )
        return self.async_show_form(
            step_id="camera_audio",
            data_schema=vol.Schema({
                vol.Optional(CONF_CAMERA_AUDIO, default=options.get(CONF_CAMERA_AUDIO, [])):
                    cameras,
                vol.Optional(CONF_NO_PREBUFFER, default=options.get(CONF_NO_PREBUFFER, [])):
                    cameras,
            }),
        )

    async def async_step_alarm_codes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A code per alarm panel, sent when the Home app arms or disarms it."""
        panels = sorted(
            state.entity_id for state in self.hass.states.async_all("alarm_control_panel")
            if state.attributes.get("code_format") is not None
            or state.attributes.get("code_arm_required")
        )
        if not panels:
            return self.async_abort(reason="no_alarm_codes")
        codes = dict(self.config_entry.options.get(CONF_ALARM_CODES) or {})
        if user_input is not None:
            for entity_id in panels:
                if code := str(user_input.get(entity_id) or "").strip():
                    codes[entity_id] = code
                else:
                    codes.pop(entity_id, None)
            return self._save(**{CONF_ALARM_CODES: codes})
        password = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
        return self.async_show_form(
            step_id="alarm_codes",
            data_schema=vol.Schema({
                vol.Optional(
                    entity_id, description={"suggested_value": codes.get(entity_id)}
                ): password
                for entity_id in panels
            }),
        )
