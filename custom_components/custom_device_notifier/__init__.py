"""The Custom Device Notifier integration."""
from __future__ import annotations

from typing import Any, Mapping
from types import MappingProxyType

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType, HomeAssistantType, CALLBACK_TYPE

from .const import (
    DOMAIN,
    CONF_TARGETS,
    CONF_FALLBACK,
    SENSOR_ID,
)
from .sensor import CustomDeviceNotifierSensor
from .notify import CustomDeviceNotifierManager

PLATFORMS = ["sensor", "notify"]

async def async_setup(hass: HomeAssistantType, config: ConfigType) -> bool:
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Merge options and data safely
    merged_data: dict[str, Any] = {
        **dict(entry.options or {}),
        **dict(entry.data or {}),
    }

    # Store data for access
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "merged": merged_data,
        "manager": CustomDeviceNotifierManager(hass, merged_data),
        "unsub_update_listener": None,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
        await hass.config_entries.async_reload(entry.entry_id)

    unsub = entry.add_update_listener(_reload_on_update)
    hass.data[DOMAIN][entry.entry_id]["unsub_update_listener"] = unsub

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, {})
        unsub: CALLBACK_TYPE | None = data.get("unsub_update_listener")
        if unsub:
            unsub()

    return unload_ok
