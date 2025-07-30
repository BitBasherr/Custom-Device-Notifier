"""Sensor reporting current target for Custom Device Notifier."""
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change

from .const import (
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    CONF_PRIORITY,
    CONF_FALLBACK,
    KEY_SERVICE,
    KEY_CONDITIONS,
    KEY_MATCH,
)
from . import _evaluate_cond

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    async_add_entities([NotifyCurrentTargetSensor(hass, entry)], update_before_add=True)

class NotifyCurrentTargetSensor(SensorEntity):
    """Shows which notify target (or fallback) will be used."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry

        raw = entry.data.get(CONF_SERVICE_NAME_RAW)
        slug = entry.data.get(CONF_SERVICE_NAME)
        self._attr_name = f"{raw} Current Target"
        self._attr_unique_id = f"{slug}_current_target"
        self._attr_state = None

        # watch all entities used in conditions
        watchers = {
            cond["entity"]
            for tgt in entry.data[CONF_TARGETS]
            for cond in tgt[KEY_CONDITIONS]
        }
        for ent in watchers:
            async_track_state_change(hass, ent, self._on_change)

    @property
    def state(self):
        return self._attr_state

    async def async_update(self):
        data = self.entry.data
        for svc in data[CONF_PRIORITY]:
            tgt = next((t for t in data[CONF_TARGETS] if t[KEY_SERVICE] == svc), None)
            if not tgt:
                continue
            mode = tgt.get(KEY_MATCH, "all")
            results = [_evaluate_cond(self.hass, c) for c in tgt[KEY_CONDITIONS]]
            matched = all(results) if mode == "all" else any(results)
            if matched:
                self._attr_state = svc
                return
        self._attr_state = data[CONF_FALLBACK]

    @callback
    def _on_change(self, *args):
        self.async_schedule_update_ha_state(True)
