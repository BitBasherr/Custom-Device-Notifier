from __future__ import annotations

from typing import Any, Dict

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
)

# Use the shared signal helper from __init__.py (single source of truth)
from . import _signal_name


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    async_add_entities([CurrentTargetSensor(hass, entry)], True)


class CurrentTargetSensor(RestoreEntity, SensorEntity):
    """Shows the *actual* last routed target, updated live via dispatcher."""

    _attr_icon = "mdi:send-circle-outline"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry

        raw_name = str(entry.data.get(CONF_SERVICE_NAME_RAW) or "Custom Notifier")
        slug = str(entry.data.get(CONF_SERVICE_NAME) or "custom_notifier")

        # Keep name/unique_id identical to your previous sensor so the entity_id is stable
        self._attr_name = f"{raw_name} Current Target"
        self._attr_unique_id = f"{slug}_current_target"

        self._attr_native_value = None
        self._attr_extra_state_attributes: Dict[str, Any] = {}

        self._unsub_signal = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.title
            or (
                self._entry.data.get(CONF_SERVICE_NAME_RAW) or "Custom Device Notifier"
            ),
            "manufacturer": "Custom Device Notifier",
            "entry_type": "service",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore last decision for nice dashboards after restart (purely cosmetic)
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_native_value = last.state
            # keep any attributes we previously stored
            self._attr_extra_state_attributes = dict(last.attributes or {})

        # Live updates from the router in __init__._route_and_forward()
        self._unsub_signal = async_dispatcher_connect(
            self.hass, _signal_name(self._entry.entry_id), self._on_route_decision
        )
        self.async_on_remove(self._unsub_signal)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_signal:
            self._unsub_signal()
            self._unsub_signal = None

    @callback
    def _on_route_decision(self, decision: Dict[str, Any]) -> None:
        """
        decision looks like:
        {
          "timestamp": "...",
          "mode": "conditional|smart",
          "payload_keys": [...],
          "result": "forwarded|dropped|dropped_self",
          "service_full": "notify.mobile_app_xyz",  # when forwarded
          "via": "matched|fallback|self-recursion-fallback",
          "smart": {...} | "conditional": {...}
        }
        """
        result = str(decision.get("result") or "").strip()

        if result == "forwarded":
            svc_full = str(decision.get("service_full") or "")
            # show short service name without domain (matches your old UI)
            try:
                _, short = svc_full.split(".", 1)
            except ValueError:
                short = svc_full
            if short.startswith("notify."):
                short = short[len("notify.") :]
            new_state = short or "—"
        else:
            # dropped / dropped_self (or anything unexpected)
            new_state = result or "—"

        # Surface helpful context; keep nested blocks for debugging
        attrs: Dict[str, Any] = {
            "timestamp": decision.get("timestamp"),
            "mode": decision.get("mode"),
            "via": decision.get("via"),
            "payload_keys": decision.get("payload_keys", []),
            "service_full": decision.get("service_full"),
        }
        if "smart" in decision:
            attrs["smart"] = decision["smart"]
        if "conditional" in decision:
            attrs["conditional"] = decision["conditional"]

        self._attr_native_value = new_state
        self._attr_extra_state_attributes = attrs
        self.async_write_ha_state()
