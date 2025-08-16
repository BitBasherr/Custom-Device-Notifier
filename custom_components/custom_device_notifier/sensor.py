from __future__ import annotations

import logging
from typing import Any, Mapping, Callable

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    CONF_FALLBACK,
    CONF_PRIORITY,
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    DOMAIN,
)

_LOGGER = logging.getLogger(DOMAIN)


def _get_entry_data(entry: ConfigEntry) -> Mapping[str, Any]:
    return entry.options or entry.data


def _signal_name(entry_id: str) -> str:
    return f"{DOMAIN}_route_update_{entry_id}"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    async_add_entities([CurrentRouteSensor(hass, entry)])


class CurrentRouteSensor(SensorEntity):
    """Shows the last *actual* notify target chosen by the integration."""

    _attr_icon = "mdi:send"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry

        data = _get_entry_data(entry)
        raw_name = data.get(CONF_SERVICE_NAME_RAW) or data.get(CONF_SERVICE_NAME) or "Notifier"
        slug = data.get(CONF_SERVICE_NAME) or "custom_notifier"

        self._attr_name = f"{raw_name} Last Route"
        self._attr_unique_id = f"{slug}_last_route"
        self._attr_native_value = None
        self._attrs: dict[str, Any] = {}
        self._unsub_dispatch: Callable[[], None] | None = None

        # If nothing has been routed yet, show fallback so the sensor isn't empty
        fb = str(data.get(CONF_FALLBACK) or "")
        self._attr_native_value = fb.removeprefix("notify.")
        self._attrs["via"] = "startup"
        self._attrs["pending_first_decision"] = True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._attrs)

    async def async_added_to_hass(self) -> None:
        self._unsub_dispatch = async_dispatcher_connect(
            self.hass, _signal_name(self._entry.entry_id), self._on_decision
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_dispatch:
            self._unsub_dispatch()
            self._unsub_dispatch = None

    @callback
    def _on_decision(self, decision: dict[str, Any]) -> None:
        """Receive routing decisions from the notifier and update state."""
        # decision keys set by __init__._route_and_forward:
        #   result: "forwarded" | "dropped" | ...
        #   service_full: "domain.service"
        #   via: "matched" | "fallback" | "self-recursion-fallback"
        #   mode: "conditional" | "smart"
        #   conditional / smart: {...}
        result = decision.get("result")
        if result != "forwarded":
            # Keep state but expose last drop reason for visibility
            self._attrs.update({
                "last_result": result,
                "timestamp": decision.get("timestamp"),
            })
            self.async_write_ha_state()
            return

        svc_full = str(decision.get("service_full") or "")
        svc_short = svc_full.split(".", 1)[1] if "." in svc_full else svc_full

        self._attr_native_value = svc_short
        self._attrs.update({
            "mode": decision.get("mode"),
            "via": decision.get("via"),
            "timestamp": decision.get("timestamp"),
            "payload_keys": decision.get("payload_keys", []),
            "smart": decision.get("smart"),
            "conditional": decision.get("conditional"),
            "last_result": result,
            "pending_first_decision": False,
        })
        self.async_write_ha_state()
