import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    DOMAIN,
    CONF_SERVICE_NAME_RAW,
    CONF_SERVICE_NAME,
    CONF_TARGETS,
    CONF_PRIORITY,
    CONF_FALLBACK,
    KEY_SERVICE,
    KEY_CONDITIONS,
    KEY_MATCH,
)
from .__init__ import _evaluate_cond

_LOGGER = logging.getLogger(DOMAIN)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    async_add_entities([CurrentTargetSensor(hass, entry)])

class CurrentTargetSensor(SensorEntity):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass       = hass
        self._entry     = entry
        data            = entry.data
        self._targets   = data[CONF_TARGETS]
        self._priority  = data[CONF_PRIORITY]
        self._fallback  = data[CONF_FALLBACK]
        raw_name        = data[CONF_SERVICE_NAME_RAW]
        slug            = data[CONF_SERVICE_NAME]
        self._attr_name      = f"{raw_name} Current Target"
        self._attr_unique_id = f"{slug}_current_target"
        self._state          = None

    async def async_added_to_hass(self):
        entities = {
            cond["entity"]
            for tgt in self._targets
            for cond in tgt[KEY_CONDITIONS]
        }
        async_track_state_change_event(self.hass, list(entities), self._update)
        self._update(None)

    @callback
    def _update(self, _):
        for svc_id in self._priority:
            tgt = next((t for t in self._targets if t[KEY_SERVICE] == svc_id), None)
            if not tgt:
                continue
            mode = tgt.get(KEY_MATCH, "all")
            results = [_evaluate_cond(self.hass, c) for c in tgt[KEY_CONDITIONS]]
            matched = all(results) if mode == "all" else any(results)
            if matched:
                self._state = svc_id
                break
        else:
            self._state = self._fallback

        self.async_write_ha_state()
