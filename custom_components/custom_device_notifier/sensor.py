from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Mapping, Optional

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FALLBACK,
    CONF_PRIORITY,
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_MATCH,
    KEY_SERVICE,
)
from .evaluate import evaluate_condition

_LOGGER = logging.getLogger(DOMAIN)


def _get_entry_data(entry: ConfigEntry) -> Mapping[str, Any]:
    """Prefer options over data; options flow writes there."""
    return entry.options or entry.data


def _signal_name(entry_id: str) -> str:
    """Dispatcher signal used by __init__.py to publish routing decisions."""
    return f"{DOMAIN}_route_update_{entry_id}"


class CurrentTargetSensor(SensorEntity):
    """Shows the currently chosen target service (actual routed decision when available)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry

        data = _get_entry_data(entry)
        self._targets = data[CONF_TARGETS]
        self._priority = data[CONF_PRIORITY]
        self._fallback = data[CONF_FALLBACK]

        raw_name = data[CONF_SERVICE_NAME_RAW]
        slug = data[CONF_SERVICE_NAME]

        self._attr_name = f"{raw_name} Current Target"
        self._attr_unique_id = f"{slug}_current_target"
        self._attr_native_value: Optional[str] = None
        self._attr_extra_state_attributes = {}

        # Subscriptions
        self._unsub_state_change: Callable[[], None] | None = None
        self._unsub_signal: Callable[[], None] | None = None

        # To avoid immediately overwriting a fresh router decision with a background eval
        self._last_decision_at = None  # datetime | None

    async def async_added_to_hass(self) -> None:
        # Listen for entity changes used by conditions (background “would choose” updates)
        entities = {
            cond["entity_id"]
            for tgt in self._targets
            for cond in tgt[KEY_CONDITIONS]
            if self.hass.states.get(cond["entity_id"]) is not None
        }
        self._unsub_state_change = async_track_state_change_event(
            self.hass, list(entities), self._update_from_entities
        )

        # Listen for actual routing decisions from __init__.py
        self._unsub_signal = async_dispatcher_connect(
            self.hass, _signal_name(self._entry.entry_id), self._on_route_decision
        )

        # Initial background evaluation
        await self._async_evaluate_and_update()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state_change:
            self._unsub_state_change()
            self._unsub_state_change = None
        if self._unsub_signal:
            self._unsub_signal()
            self._unsub_signal = None

    # ── updates from entity state changes (background prediction) ──

    @callback
    def _update_from_entities(self, _) -> None:
        self.hass.async_create_task(self._async_evaluate_and_update())

    async def _async_evaluate_and_update(self) -> None:
        """Predict ‘current’ target from conditions/priority for idle display."""
        # If we just received an actual decision very recently, don’t clobber it.
        if self._last_decision_at:
            if (dt_util.utcnow() - self._last_decision_at) <= timedelta(seconds=2):
                return

        new_value = self._fallback  # default

        for svc_id in self._priority:
            tgt = next((t for t in self._targets if t[KEY_SERVICE] == svc_id), None)
            if not tgt:
                continue

            mode = tgt.get(KEY_MATCH, "all")
            results = await asyncio.gather(
                *(evaluate_condition(self.hass, c) for c in tgt[KEY_CONDITIONS])
            )
            matched = all(results) if mode == "all" else any(results)
            if matched:
                new_value = svc_id
                break

        if isinstance(new_value, str) and new_value.startswith("notify."):
            new_value = new_value[len("notify.") :]

        self._attr_native_value = new_value
        self.async_write_ha_state()

    # ── updates from the router (actual send decisions) ──

    @callback
    def _on_route_decision(self, decision: dict) -> None:
        """
        Called by dispatcher whenever the router forwards a notification.
        decision example:
          {
            "timestamp": "...",
            "result": "forwarded",
            "service_full": "notify.mobile_app_pixel_7",
            "via": "matched" | "fallback" | "self-recursion-fallback",
            "mode": "smart" | "conditional",
            "smart": {...} | "conditional": {...}
          }
        """
        svc_full = decision.get("service_full")
        if not svc_full:
            return

        # Record when this arrived so background eval won’t instantly overwrite it
        self._last_decision_at = dt_util.utcnow()

        # Present the human-friendly service short name (without domain)
        try:
            _, short = svc_full.split(".", 1)
        except ValueError:
            short = svc_full
        if short.startswith("notify."):
            short = short[len("notify.") :]

        self._attr_native_value = short

        # Optional: surface a bit of context for debugging
        attrs = dict(self._attr_extra_state_attributes or {})
        attrs.update(
            {
                "last_decision_ts": decision.get("timestamp"),
                "last_decision_via": decision.get("via"),
                "last_decision_mode": decision.get("mode"),
                "last_decision_service": svc_full,
            }
        )
        self._attr_extra_state_attributes = attrs

        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    async_add_entities([CurrentTargetSensor(hass, entry)])
