from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.components.network import async_get_adapters
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    DEFAULT_CREDENTIAL_FILE_PATH,
    DEFAULT_NOTIFICATION_TITLE,
    DEFAULT_TITLE,
    DEFAULT_TRIGGER_ON_STATE,
    DOMAIN,
    RUNTIME_UNIQUE_ID,
)


def _validate_input(data: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}

    api_url = str(data.get("api_url", "")).strip()
    parsed = urlparse(api_url)
    if not api_url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        errors["api_url"] = "invalid_api_url"

    if not str(data.get("device_no", "")).strip():
        errors["device_no"] = "required_field"

    if not str(data.get("func_name", "")).strip():
        errors["func_name"] = "required_field"

    trigger_entity_id = str(data.get("trigger_entity_id", "")).strip()
    if trigger_entity_id and "." not in trigger_entity_id:
        errors["trigger_entity_id"] = "invalid_entity_id"

    status_entity_id = str(data.get("status_entity_id", "")).strip()
    if status_entity_id and "." not in status_entity_id:
        errors["status_entity_id"] = "invalid_entity_id"

    return errors


def _build_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("api_url", default=defaults.get("api_url", "")): str,
            vol.Required("device_no", default=defaults.get("device_no", "")): str,
            vol.Required("func_name", default=defaults.get("func_name", "")): str,
            vol.Optional("ipv4_address", default=defaults.get("ipv4_address", "")): str,
            vol.Optional("mac_address", default=defaults.get("mac_address", "")): str,
            vol.Optional("enabled", default=defaults.get("enabled", True)): bool,
            vol.Optional("run_on_startup", default=defaults.get("run_on_startup", True)): bool,
            vol.Optional("allow_reregister", default=defaults.get("allow_reregister", False)): bool,
            vol.Optional("trigger_entity_id", default=defaults.get("trigger_entity_id", "")): str,
            vol.Optional("trigger_on_state", default=defaults.get("trigger_on_state", DEFAULT_TRIGGER_ON_STATE)): str,
            vol.Optional("reset_trigger_after_run", default=defaults.get("reset_trigger_after_run", True)): bool,
            vol.Optional("status_entity_id", default=defaults.get("status_entity_id", "")): str,
            vol.Optional(
                "publish_persistent_notification",
                default=defaults.get("publish_persistent_notification", True),
            ): bool,
            vol.Optional(
                "notification_title",
                default=defaults.get("notification_title", DEFAULT_NOTIFICATION_TITLE),
            ): str,
            vol.Optional(
                "credential_file_path",
                default=defaults.get("credential_file_path", DEFAULT_CREDENTIAL_FILE_PATH),
            ): str,
        }
    )


async def _async_detect_network_defaults(hass) -> dict[str, str]:
    """Detect default IPv4 and MAC from HA network adapters."""
    try:
        adapters = await async_get_adapters(hass)
    except Exception:
        return {}

    for adapter in adapters:
        ipv4_entries = adapter.get("ipv4", [])
        ipv4_address = ""
        if ipv4_entries:
            first_ipv4 = ipv4_entries[0]
            if isinstance(first_ipv4, dict):
                ipv4_address = str(first_ipv4.get("address", "")).strip()

        mac_address = str(adapter.get("mac_address", "")).strip()

        if ipv4_address or mac_address:
            detected: dict[str, str] = {}
            if ipv4_address:
                detected["ipv4_address"] = ipv4_address
            if mac_address:
                detected["mac_address"] = mac_address
            return detected

    return {}


async def _async_defaults_with_network(hass, base_defaults: dict[str, Any]) -> dict[str, Any]:
    """Merge detected network values only when form defaults are empty."""
    defaults = dict(base_defaults)
    detected = await _async_detect_network_defaults(hass)
    for key in ("ipv4_address", "mac_address"):
        if not str(defaults.get(key, "")).strip() and key in detected:
            defaults[key] = detected[key]
    return defaults


class HugCareGatewayConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HugCare Gateway."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_input(user_input)
            if not errors:
                await self.async_set_unique_id(RUNTIME_UNIQUE_ID)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=DEFAULT_TITLE, data=user_input)

        defaults = user_input or await _async_defaults_with_network(self.hass, {})

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(defaults),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_input(user_input)
            if not errors:
                await self.async_set_unique_id(RUNTIME_UNIQUE_ID)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data_updates=user_input)

        defaults = user_input or await _async_defaults_with_network(self.hass, dict(entry.data))

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_schema(defaults),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return HugCareGatewayOptionsFlow(config_entry)


class HugCareGatewayOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for HugCare Gateway."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_input(user_input)
            if not errors:
                self.hass.config_entries.async_update_entry(self._config_entry, data=user_input)
                return self.async_create_entry(title="", data={})

        defaults = user_input or await _async_defaults_with_network(self.hass, dict(self._config_entry.data))

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(defaults),
            errors=errors,
        )
