from __future__ import annotations

from typing import Any, Dict, Optional, Callable, List

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.device_registry import DeviceEntryType

from .const import (
    DOMAIN,
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_MEDICATIONS,
    CONF_MED_NAME,
    CONF_MED_SCHEDULE,
)

# If you prefer to avoid importing from __init__, copy the helper here instead.
from . import _signal_name


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    entities: List[SensorEntity] = [CurrentTargetSensor(hass, entry)]
    
    # Add medication sensors if configured
    medications = entry.options.get(CONF_MEDICATIONS, [])
    if medications:
        from .medication_sensor import MedicationSensor
        
        for med_config in medications:
            med_name = med_config.get(CONF_MED_NAME, "")
            schedule = med_config.get(CONF_MED_SCHEDULE, [])
            if med_name:
                entities.append(MedicationSensor(hass, entry, med_name, schedule))
    
    async_add_entities(entities, True)


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

        # Properly typed unsubscribe handle
        self._unsub_signal: Optional[Callable[[], None]] = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.title
            or (
                self._entry.data.get(CONF_SERVICE_NAME_RAW) or "Custom Device Notifier"
            ),
            "manufacturer": "Custom Device Notifier",
            "entry_type": DeviceEntryType.SERVICE,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore last decision for nice dashboards after restart (purely cosmetic)
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_native_value = last.state
            self._attr_extra_state_attributes = dict(last.attributes or {})

        # Live updates from the router in __init__._route_and_forward()
        unsub = async_dispatcher_connect(
            self.hass, _signal_name(self._entry.entry_id), self._on_route_decision
        )
        self._unsub_signal = unsub
        # async_on_remove requires a non-Optional callable
        self.async_on_remove(unsub)

    async def async_will_remove_from_hass(self) -> None:
        # Be defensive for runtime safety
        if self._unsub_signal is not None:
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
            # show short service name without domain
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
