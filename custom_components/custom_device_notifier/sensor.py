from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Dict, Optional

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_FALLBACK,
    CONF_PRIORITY,
    CONF_ROUTING_MODE,
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    CONF_SMART_MIN_BATTERY,
    CONF_SMART_PC_FRESH_S,
    CONF_SMART_PC_NOTIFY,
    CONF_SMART_PC_SESSION,
    CONF_SMART_PHONE_FRESH_S,
    CONF_SMART_PHONE_ORDER,
    CONF_SMART_POLICY,
    CONF_SMART_REQUIRE_AWAKE,
    CONF_SMART_REQUIRE_PHONE_UNLOCKED,
    CONF_SMART_REQUIRE_UNLOCKED,
    DEFAULT_ROUTING_MODE,
    DEFAULT_SMART_MIN_BATTERY,
    DEFAULT_SMART_PC_FRESH_S,
    DEFAULT_SMART_PHONE_FRESH_S,
    DEFAULT_SMART_POLICY,
    DEFAULT_SMART_REQUIRE_AWAKE,
    DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED,
    DEFAULT_SMART_REQUIRE_UNLOCKED,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_MATCH,
    KEY_SERVICE,
    ROUTING_CONDITIONAL,
    ROUTING_SMART,
)
# If you prefer to avoid importing from __init__, copy the helper here instead.
from . import _signal_name
from .evaluate import evaluate_condition
from .smart_select import choose_best_target

_LOGGER = logging.getLogger(DOMAIN)

def _get_entry_data(entry: ConfigEntry) -> Dict[str, Any]:
    """Prefer options over data; options flow writes there."""
    return entry.options or entry.data

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    async_add_entities([CurrentTargetSensor(hass, entry)], True)

class CurrentTargetSensor(RestoreEntity, SensorEntity):
    """Shows a live preview of what target would be used now, updated on state changes."""
    _attr_icon = "mdi:send-circle-outline"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._data = _get_entry_data(entry)

        raw_name = str(self._data.get(CONF_SERVICE_NAME_RAW) or "Custom Notifier")
        slug = str(self._data.get(CONF_SERVICE_NAME) or "custom_notifier")

        # Keep name/unique_id identical to your previous sensor so the entity_id is stable
        self._attr_name = f"{raw_name} Current Target"
        self._attr_unique_id = f"{slug}_current_target"

        self._attr_native_value = None
        self._attr_extra_state_attributes: Dict[str, Any] = {}

        # Properly typed unsubscribe handles
        self._unsub_signal: Optional[Callable[[], None]] = None
        self._unsub_track: Optional[Callable[[], None]] = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.title or (
                self._entry.data.get(CONF_SERVICE_NAME_RAW) or "Custom Device Notifier"
            ),
            "manufacturer": "Custom Device Notifier",
            "entry_type": "service",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Restore last state for continuity after restart (purely cosmetic)
        last = await self.async_get_last_state()
        if last is not None:
            self._attr_native_value = last.state
            self._attr_extra_state_attributes = dict(last.attributes or {})

        # Live updates from the router in __init__._route_and_forward()
        unsub_signal = async_dispatcher_connect(
            self.hass, _signal_name(self._entry.entry_id), self._on_route_decision
        )
        self._unsub_signal = unsub_signal
        self.async_on_remove(unsub_signal)

        # Set up proactive/live preview updates based on mode
        mode = self._data.get(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE)
        entities = set()

        if mode == ROUTING_CONDITIONAL:
            self._targets = self._data.get(CONF_TARGETS, [])
            self._priority = self._data.get(CONF_PRIORITY, [])
            self._fallback = self._data.get(CONF_FALLBACK)
            entities = {
                cond["entity_id"]
                for tgt in self._targets
                for cond in tgt.get(KEY_CONDITIONS, [])
                if self.hass.states.get(cond["entity_id"]) is not None
            }
        elif mode == ROUTING_SMART:
            pc_session = self._data.get(CONF_SMART_PC_SESSION)
            if pc_session and self.hass.states.get(pc_session):
                entities.add(pc_session)
            phone_order = self._data.get(CONF_SMART_PHONE_ORDER, [])
            for svc in phone_order:
                spec = _spec_from_service(svc)  # Assuming this helper is in smart_select.py or similar
                if spec:
                    if spec.last_trigger and self.hass.states.get(spec.last_trigger):
                        entities.add(spec.last_trigger)
                    if spec.locked_binary and self.hass.states.get(spec.locked_binary):
                        entities.add(spec.locked_binary)
                    if spec.battery_sensor and self.hass.states.get(spec.battery_sensor):
                        entities.add(spec.battery_sensor)

        if entities:
            unsub_track = async_track_state_change_event(
                self.hass, list(entities), self._update
            )
            self._unsub_track = unsub_track
            self.async_on_remove(unsub_track)

        # Initial preview update
        await self._async_evaluate_and_update()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_signal is not None:
            self._unsub_signal()
            self._unsub_signal = None
        if self._unsub_track is not None:
            self._unsub_track()
            self._unsub_track = None

    @callback
    def _update(self, _) -> None:
        self.hass.async_create_task(self._async_evaluate_and_update())

    async def _async_evaluate_and_update(self) -> None:
        mode = self._data.get(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE)
        new_value = "none"  # Default if no match

        if mode == ROUTING_CONDITIONAL:
            new_value = self._fallback or "none"
            for svc_id in self._priority:
                tgt = next((t for t in self._targets if t[KEY_SERVICE] == svc_id), None)
                if not tgt:
                    continue
                cond_mode = tgt.get(KEY_MATCH, "all")
                results = await asyncio.gather(
                    *(evaluate_condition(self.hass, c) for c in tgt.get(KEY_CONDITIONS, []))
                )
                matched = all(results) if cond_mode == "all" else any(results) if cond_mode == "any" else False
                if matched:
                    new_value = svc_id
                    break
        elif mode == ROUTING_SMART:
            pc_notify = self._data.get(CONF_SMART_PC_NOTIFY)
            pc_session = self._data.get(CONF_SMART_PC_SESSION)
            phone_order = self._data.get(CONF_SMART_PHONE_ORDER, [])
            min_batt = self._data.get(CONF_SMART_MIN_BATTERY, DEFAULT_SMART_MIN_BATTERY)
            phone_fresh = self._data.get(CONF_SMART_PHONE_FRESH_S, DEFAULT_SMART_PHONE_FRESH_S)
            pc_fresh = self._data.get(CONF_SMART_PC_FRESH_S, DEFAULT_SMART_PC_FRESH_S)
            require_awake = self._data.get(CONF_SMART_REQUIRE_AWAKE, DEFAULT_SMART_REQUIRE_AWAKE)
            # Using phone-specific unlocked param based on const/translations; adjust if your routing logic differs
            require_unlocked = self._data.get(CONF_SMART_REQUIRE_PHONE_UNLOCKED, DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED)
            policy = self._data.get(CONF_SMART_POLICY, DEFAULT_SMART_POLICY)

            target = choose_best_target(
                self.hass,
                pc_notify_target=pc_notify,
                pc_session_sensor=pc_session,
                phones_in_priority=phone_order,
                min_battery=min_batt,
                phone_fresh_s=phone_fresh,
                pc_fresh_s=pc_fresh,
                require_awake=require_awake,
                require_unlocked=require_unlocked,
                policy=policy,
            )
            if target:
                new_value = target

        # Shorten the value (e.g., remove "notify.")
        if new_value.startswith("notify."):
            new_value = new_value[len("notify.") :]

        self._attr_native_value = new_value
        self.async_write_ha_state()

    @callback
    def _on_route_decision(self, decision: Dict[str, Any]) -> None:
        """Update attributes with last actual decision details, but don't override the live preview value."""
        result = str(decision.get("result") or "").strip()

        last_target = "—"
        if result == "forwarded":
            svc_full = str(decision.get("service_full") or "")
            # Show short service name without domain
            try:
                _, short = svc_full.split(".", 1)
            except ValueError:
                short = svc_full
            if short.startswith("mobile_app_"):
                short = short[len("mobile_app_") :]
            last_target = short or "—"
        else:
            # dropped / dropped_self (or anything unexpected)
            last_target = result or "—"

        attrs: Dict[str, Any] = {
            "last_timestamp": decision.get("timestamp"),
            "last_mode": decision.get("mode"),
            "last_via": decision.get("via"),
            "last_result": result,
            "last_payload_keys": decision.get("payload_keys", []),
            "last_service_full": decision.get("service_full"),
            "last_target": last_target,
        }
        if "smart" in decision:
            attrs["last_smart"] = decision["smart"]
        if "conditional" in decision:
            attrs["last_conditional"] = decision["conditional"]

        self._attr_extra_state_attributes.update(attrs)
        self.async_write_ha_state()