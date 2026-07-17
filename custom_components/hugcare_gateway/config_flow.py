from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse
import asyncio
import uuid
import ipaddress
import re
from pathlib import Path

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

_LOGGER = logging.getLogger(__name__)


def _normalize_mac(raw: Any) -> str:
    """Normalize MAC to AA:BB:CC:DD:EE:FF format when possible."""
    text = str(raw or "").strip().replace("-", ":")
    if not text:
        return ""

    compact = text.replace(":", "")
    if len(compact) == 12 and all(c in "0123456789abcdefABCDEF" for c in compact):
        compact = compact.upper()
        return ":".join(compact[i : i + 2] for i in range(0, 12, 2))

    return ""


def _is_preferred_ipv4(value: str) -> bool:
    """Check if an IPv4 value is usable for runtime defaults."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False

    return (
        ip.version == 4
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_unspecified
    )


def _is_noisy_virtual_interface(name: str) -> bool:
    """Detect virtual/container interfaces we should deprioritize."""
    lowered = name.lower()
    prefixes = (
        "veth",
        "docker",
        "br-",
        "virbr",
        "cni",
        "hassio",
        "tailscale",
        "wg",
        "zt",
        "tun",
        "tap",
        "lo",
    )
    return lowered.startswith(prefixes)


def _extract_mac_from_nested(data: Any) -> str:
    """Search for a MAC-like value in nested adapter payload."""
    if isinstance(data, dict):
        for key in ("mac_address", "mac", "hw_address", "hwaddress", "address"):
            value = data.get(key)
            mac = _normalize_mac(value)
            if mac and mac.count(":") == 5:
                return mac

        for value in data.values():
            mac = _extract_mac_from_nested(value)
            if mac:
                return mac

    if isinstance(data, list):
        for item in data:
            mac = _extract_mac_from_nested(item)
            if mac:
                return mac

    return ""


async def _async_network_from_ha_cli(target_ipv4: str | None = None) -> dict[str, str]:
    """Try to resolve IPv4 and MAC from `ha network info`."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ha",
            "network",
            "info",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    except Exception:
        return {}

    if process.returncode != 0:
        _LOGGER.debug("HugCare ha cli fallback failed: %s", stderr.decode(errors="ignore").strip())
        return {}

    output = stdout.decode(errors="ignore")

    candidates: list[dict[str, Any]] = []
    current_mac = ""
    current_ipv4s: list[str] = []
    ipv4_pattern = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")

    def _commit_current() -> None:
        if current_mac or current_ipv4s:
            candidates.append({"mac": current_mac, "ipv4s": list(current_ipv4s)})

    for line in output.splitlines():
        stripped = line.strip()

        if stripped.startswith("- interface:") or stripped.startswith("interface:"):
            _commit_current()
            current_mac = ""
            current_ipv4s = []
            continue

        if stripped.lower().startswith("mac:"):
            parts = stripped.split(":", 1)
            if len(parts) == 2:
                mac = _normalize_mac(parts[1])
                if mac:
                    current_mac = mac
            continue

        match = ipv4_pattern.search(stripped)
        if match:
            ipv4 = match.group(0).split("/", 1)[0]
            if _is_preferred_ipv4(ipv4):
                current_ipv4s.append(ipv4)

    _commit_current()

    if target_ipv4:
        for candidate in candidates:
            if target_ipv4 in candidate["ipv4s"]:
                detected: dict[str, str] = {"ipv4_address": target_ipv4}
                if candidate["mac"]:
                    detected["mac_address"] = candidate["mac"]
                return detected

    for candidate in candidates:
        if candidate["ipv4s"] and candidate["mac"]:
            return {
                "ipv4_address": candidate["ipv4s"][0],
                "mac_address": candidate["mac"],
            }

    for candidate in candidates:
        if candidate["ipv4s"]:
            return {"ipv4_address": candidate["ipv4s"][0]}

    for candidate in candidates:
        if candidate["mac"]:
            return {"mac_address": candidate["mac"]}

    # Last resort: parse globally like shell pipeline behavior.
    first_ipv4 = ""
    first_mac = ""
    for line in output.splitlines():
        if not first_ipv4:
            match = ipv4_pattern.search(line)
            if match:
                ipv4 = match.group(0).split("/", 1)[0]
                if _is_preferred_ipv4(ipv4):
                    first_ipv4 = ipv4

        if not first_mac:
            stripped = line.strip()
            if stripped.lower().startswith("mac:"):
                parts = stripped.split(":", 1)
                if len(parts) == 2:
                    first_mac = _normalize_mac(parts[1])

        if first_ipv4 and first_mac:
            break

    detected: dict[str, str] = {}
    if first_ipv4:
        detected["ipv4_address"] = first_ipv4
    if first_mac:
        detected["mac_address"] = first_mac
    return detected


def _validate_input(data: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}

    api_url = str(data.get("api_url", "")).strip()
    if api_url:
        parsed = urlparse(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
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
            vol.Optional("api_url", default=defaults.get("api_url", "")): str,
            vol.Optional("api_key", default=defaults.get("api_key", "")): str,
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
        _LOGGER.exception("Failed to query network adapters for HugCare defaults")
        return {}

    def _extract_ipv4(adapter: dict[str, Any]) -> str:
        def _pick_best_ipv4(addresses: list[str]) -> str:
            for addr in addresses:
                if _is_preferred_ipv4(addr):
                    return addr
            return addresses[0] if addresses else ""

        ipv4_value = adapter.get("ipv4")
        if isinstance(ipv4_value, list) and ipv4_value:
            collected: list[str] = []
            for item in ipv4_value:
                if isinstance(item, dict):
                    raw = str(item.get("address", "")).strip()
                    if raw:
                        collected.append(raw.split("/", 1)[0])
                else:
                    raw = str(item).strip()
                    if raw:
                        collected.append(raw.split("/", 1)[0])
            return _pick_best_ipv4(collected)

        if isinstance(ipv4_value, dict):
            address = ipv4_value.get("address")
            if isinstance(address, list) and address:
                collected = [str(a).strip().split("/", 1)[0] for a in address if str(a).strip()]
                return _pick_best_ipv4(collected)
            raw = str(address or "").strip()
            return raw.split("/", 1)[0]

        if isinstance(ipv4_value, str):
            return ipv4_value.strip().split("/", 1)[0]

        return ""

    def _extract_mac(adapter: dict[str, Any]) -> str:
        for key in ("mac_address", "mac", "hw_address", "hwaddress"):
            mac = _normalize_mac(adapter.get(key, ""))
            if mac:
                return mac

        nested_mac = _extract_mac_from_nested(adapter)
        if nested_mac:
            return nested_mac

        return ""

    def _fallback_machine_mac() -> str:
        node = uuid.getnode()
        if node in (0, 0xFFFFFFFFFFFF):
            return ""

        # LSB of first octet indicates multicast; ignore obviously invalid node ids.
        if (node >> 40) & 0x01:
            return ""

        raw = f"{node:012X}"
        return ":".join(raw[i : i + 2] for i in range(0, 12, 2))

    def _read_mac_from_sysfs_sync(interface_name: str) -> str:
        if not interface_name:
            return ""

        try:
            mac_path = Path("/sys/class/net") / interface_name / "address"
            if not mac_path.exists():
                _LOGGER.debug(
                    "HugCare adapter diagnostic sysfs path not found for interface=%s path=%s",
                    interface_name,
                    mac_path,
                )
                return ""
            raw = mac_path.read_text(encoding="utf-8").strip()
            mac = _normalize_mac(raw)
            if mac and mac != "00:00:00:00:00:00":
                return mac
            _LOGGER.debug(
                "HugCare adapter diagnostic sysfs returned invalid mac interface=%s raw=%s",
                interface_name,
                raw,
            )
        except Exception:
            _LOGGER.exception(
                "HugCare adapter diagnostic failed reading sysfs mac for interface=%s",
                interface_name,
            )
            return ""

        return ""

    async def _async_mac_from_ip_link(interface_name: str) -> str:
        if not interface_name:
            return ""

        try:
            process = await asyncio.create_subprocess_exec(
                "ip",
                "link",
                "show",
                interface_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        except Exception:
            return ""

        if process.returncode != 0:
            _LOGGER.debug(
                "HugCare adapter diagnostic ip link failed interface=%s err=%s",
                interface_name,
                stderr.decode(errors="ignore").strip(),
            )
            return ""

        output = stdout.decode(errors="ignore")
        match = re.search(r"link/ether\s+([0-9a-fA-F:]{17})", output)
        if not match:
            _LOGGER.debug(
                "HugCare adapter diagnostic ip link no mac found interface=%s output=%s",
                interface_name,
                output.replace("\n", " | "),
            )
            return ""

        return _normalize_mac(match.group(1))

    def _adapter_priority(adapter: dict[str, Any]) -> tuple[int, int, int, int, str]:
        name = str(adapter.get("name", ""))
        enabled_rank = 0 if adapter.get("enabled", True) else 1
        noisy_rank = 1 if _is_noisy_virtual_interface(name) else 0
        has_ipv4_rank = 0 if _extract_ipv4(adapter) else 1
        default_rank = 0 if adapter.get("default") else 1
        return (enabled_rank, noisy_rank, has_ipv4_rank, default_rank, name)

    default_first = sorted(adapters, key=_adapter_priority)

    best_with_ipv4_and_mac: dict[str, str] | None = None
    best_with_ipv4: dict[str, str] | None = None
    first_mac_only: str = ""

    for index, adapter in enumerate(default_first):
        adapter_name = str(adapter.get("name", ""))
        if not adapter.get("enabled", True):
            _LOGGER.debug(
                "HugCare adapter diagnostic skip disabled adapter index=%s name=%s",
                index,
                adapter_name,
            )
            continue

        if _is_noisy_virtual_interface(adapter_name):
            _LOGGER.debug(
                "HugCare adapter diagnostic skip noisy interface index=%s name=%s",
                index,
                adapter_name,
            )
            continue

        mac_raw = {
            key: adapter.get(key)
            for key in ("mac_address", "mac", "hw_address", "hwaddress")
            if key in adapter
        }
        _LOGGER.debug(
            "HugCare adapter diagnostic index=%s name=%s default=%s enabled=%s keys=%s ipv4_raw=%s mac_raw=%s",
            index,
            adapter.get("name"),
            adapter.get("default"),
            adapter.get("enabled"),
            sorted(adapter.keys()),
            adapter.get("ipv4"),
            mac_raw,
        )

        ipv4_address = _extract_ipv4(adapter)
        mac_address = _extract_mac(adapter)

        if ipv4_address and not mac_address:
            mac_from_sysfs = await hass.async_add_executor_job(_read_mac_from_sysfs_sync, adapter_name)
            if mac_from_sysfs:
                mac_address = mac_from_sysfs
                _LOGGER.debug(
                    "HugCare adapter diagnostic detected_mac_from_sysfs interface=%s mac=%s",
                    adapter_name,
                    mac_address,
                )
            else:
                mac_from_ip_link = await _async_mac_from_ip_link(adapter_name)
                if mac_from_ip_link:
                    mac_address = mac_from_ip_link
                    _LOGGER.debug(
                        "HugCare adapter diagnostic detected_mac_from_ip_link interface=%s mac=%s",
                        adapter_name,
                        mac_address,
                    )

        if ipv4_address and mac_address:
            best_with_ipv4_and_mac = {"ipv4_address": ipv4_address, "mac_address": mac_address}
            _LOGGER.debug(
                "HugCare selected adapter index=%s detected_ipv4=%s detected_mac=%s",
                index,
                ipv4_address,
                mac_address,
            )
            break

        if ipv4_address and best_with_ipv4 is None:
            best_with_ipv4 = {"ipv4_address": ipv4_address}

        if mac_address and not first_mac_only:
            first_mac_only = mac_address

    if best_with_ipv4_and_mac is not None:
        return best_with_ipv4_and_mac

    if best_with_ipv4 is not None:
        target_ipv4 = best_with_ipv4["ipv4_address"]
        ha_cli_detected = await _async_network_from_ha_cli(target_ipv4)
        ha_cli_mac = ha_cli_detected.get("mac_address", "")
        if ha_cli_mac:
            best_with_ipv4["mac_address"] = ha_cli_mac
            _LOGGER.debug(
                "HugCare adapter diagnostic fallback detected_mac_from_ha_cli_for_ipv4=%s mac=%s",
                target_ipv4,
                ha_cli_mac,
            )
        else:
            _LOGGER.debug(
                "HugCare adapter diagnostic no_mac_from_ha_cli_for_ipv4=%s",
                target_ipv4,
            )
        return best_with_ipv4

    ha_cli_detected = await _async_network_from_ha_cli()
    ha_cli_mac = ha_cli_detected.get("mac_address", "")
    if ha_cli_detected:
        if first_mac_only and ha_cli_mac and first_mac_only != ha_cli_mac:
            _LOGGER.debug(
                "HugCare adapter diagnostic differing_mac_candidates adapter_mac=%s ha_cli_mac=%s",
                first_mac_only,
                ha_cli_mac,
            )
        _LOGGER.debug(
            "HugCare adapter diagnostic fallback detected_from_ha_cli ipv4=%s mac=%s",
            ha_cli_detected.get("ipv4_address", ""),
            ha_cli_mac,
        )
        return ha_cli_detected

    fallback_mac = _fallback_machine_mac()
    if fallback_mac:
        _LOGGER.debug(
            "HugCare adapter diagnostic fallback detected_mac_from_uuid=%s",
            fallback_mac,
        )
        return {"mac_address": fallback_mac}

    _LOGGER.debug("HugCare adapter diagnostic found no usable ipv4/mac values")

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

