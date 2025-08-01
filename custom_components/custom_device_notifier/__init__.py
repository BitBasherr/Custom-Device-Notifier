import logging

import voluptuous as vol
from homeassistant.components.notify import BaseNotificationService
from homeassistant.components.notify.const import ATTR_MESSAGE, ATTR_TITLE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_FALLBACK,
    CONF_PRIORITY,
    CONF_SERVICE_NAME,
    CONF_TARGETS,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_MATCH,
    KEY_SERVICE,
)
from .evaluate import evaluate_condition

_LOGGER = logging.getLogger(DOMAIN)

SERVICE_SCHEMA = vol.Schema({
    vol.Required(ATTR_MESSAGE): cv.string,
    vol.Optional(ATTR_TITLE): cv.string,
    vol.Optional("data"): dict,
})

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    try:
        data     = entry.data
        slug     = data[CONF_SERVICE_NAME]
        targets  = data[CONF_TARGETS]
        priority = data[CONF_PRIORITY]
        fallback = data[CONF_FALLBACK]

        service = _NotifierService(hass, slug, targets, priority, fallback)
        hass.services.async_register(
            "notify", slug, service.async_send_message, schema=SERVICE_SCHEMA
        )

        await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
        return True
    except Exception as e:
        _LOGGER.error("Error setting up entry: %s", e)
        return False

class _NotifierService(BaseNotificationService):
    def __init__(self, hass, slug, targets, priority, fallback):
        self.hass = hass
        self._slug = slug
        self._targets = targets
        self._priority = priority
        self._fallback = fallback

    async def async_send_message(self, message="", **kwargs):
        title = kwargs.get(ATTR_TITLE)
        data = {ATTR_MESSAGE: message}
        if title:
            data[ATTR_TITLE] = title
        data.update(kwargs.get("data", {}))

        _LOGGER.debug("notify.%s called: %s / %s", self._slug, title, message)
        for svc in self._priority:
            tgt = next((t for t in self._targets if t[KEY_SERVICE] == svc), None)
            if not tgt:
                continue
            mode = tgt.get(KEY_MATCH, "all")
            results = [evaluate_condition(self.hass, c) for c in tgt[KEY_CONDITIONS]]
            matched = all(results) if mode == "all" else any(results)
            _LOGGER.debug("  target %s match=%s", svc, matched)
            if matched:
                dom, name = svc.split(".", 1)
                _LOGGER.debug("  → forwarding to %s.%s", dom, name)
                await self.hass.services.async_call(dom, name, data, blocking=True)
                return

        dom, name = self._fallback.split(".", 1)
        _LOGGER.debug("  → fallback to %s.%s", dom, name)
        await self.hass.services.async_call(dom, name, data, blocking=True)
