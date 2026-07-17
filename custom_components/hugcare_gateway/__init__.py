from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, RUNTIME_CONFIG_PATH

_LOGGER = logging.getLogger(__name__)

HugCareConfigEntry = ConfigEntry


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the HugCare Gateway integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HugCareConfigEntry) -> bool:
    """Set up HugCare Gateway from a config entry."""
    await _async_write_runtime_config(hass, entry)
    unsubscribe = entry.add_update_listener(_async_update_listener)
    hass.data[DOMAIN][entry.entry_id] = unsubscribe
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HugCareConfigEntry) -> bool:
    """Unload a config entry."""
    unsubscribe = hass.data[DOMAIN].pop(entry.entry_id, None)
    if unsubscribe is not None:
        unsubscribe()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: HugCareConfigEntry) -> None:
    """Handle config entry updates."""
    await _async_write_runtime_config(hass, entry)


async def _async_write_runtime_config(hass: HomeAssistant, entry: HugCareConfigEntry) -> None:
    """Write config entry data to the runtime JSON file read by NetDaemon."""
    payload = {
        "enabled": entry.data.get("enabled", True),
        "runOnStartup": entry.data.get("run_on_startup", True),
        "allowReregister": entry.data.get("allow_reregister", False),
        "apiUrl": entry.data.get("api_url", ""),
        "deviceNo": entry.data.get("device_no", ""),
        "funcName": entry.data.get("func_name", ""),
        "ipv4Address": entry.data.get("ipv4_address", ""),
        "macAddress": entry.data.get("mac_address", ""),
        "triggerEntityId": entry.data.get("trigger_entity_id", ""),
        "triggerOnState": entry.data.get("trigger_on_state", "on"),
        "resetTriggerAfterRun": entry.data.get("reset_trigger_after_run", True),
        "statusEntityId": entry.data.get("status_entity_id", ""),
        "publishPersistentNotification": entry.data.get("publish_persistent_notification", True),
        "notificationTitle": entry.data.get("notification_title", "HugCare Azure Provision"),
        "credentialFilePath": entry.data.get("credential_file_path", "./data/azure/credential.json")
    }

    await hass.async_add_executor_job(_write_runtime_config_sync, payload)
    _LOGGER.info("Wrote HugCare runtime config to %s", RUNTIME_CONFIG_PATH)


def _write_runtime_config_sync(payload: dict[str, Any]) -> None:
    """Synchronously write runtime config using atomic replace."""
    runtime_path = Path(RUNTIME_CONFIG_PATH)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = runtime_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(runtime_path)
