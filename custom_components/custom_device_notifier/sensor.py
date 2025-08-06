import asyncio
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
):
    async_add_entities([CurrentTargetSensor(hass, entry)])


class CurrentTargetSensor(SensorEntity):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self._entry = entry
        data = entry.data
        self._targets = data[CONF_TARGETS]
        self._priority = data[CONF_PRIORITY]
        self._fallback = data[CONF_FALLBACK]
        raw_name = data[CONF_SERVICE_NAME_RAW]
        slug = data[CONF_SERVICE_NAME]
        self._attr_name = f"{raw_name} Current Target"
        self._attr_unique_id = f"{slug}_current_target"
        self._attr_native_value = None  # Correct attribute for state
        self._unsub = None

    async def async_added_to_hass(self):
        entities = {
            cond["entity_id"]
            for tgt in self._targets
            for cond in tgt[KEY_CONDITIONS]
            if self.hass.states.get(cond["entity_id"]) is not None
        }
        self._unsub = async_track_state_change_event(
            self.hass, list(entities), self._update
        )
        await self._async_evaluate_and_update()  # Initial update

    async def async_will_remove_from_hass(self):
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _update(self, _):
        self.hass.async_create_task(self._async_evaluate_and_update())

    async def _async_evaluate_and_update(self):
        new_value = self._fallback  # Default to fallback initially

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

        if new_value.startswith("notify."):
            new_value = new_value[len("notify.") :]

        self._attr_native_value = new_value
        self.async_write_ha_state()
