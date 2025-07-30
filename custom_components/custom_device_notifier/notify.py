# custom_components/custom_device_notifier/notify.py

"""Proper notify platform for Custom Device Notifier."""
import logging

from homeassistant.components.notify import BaseNotificationService, ATTR_MESSAGE, ATTR_TITLE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_SERVICE_NAME,
    CONF_TARGETS,
    CONF_PRIORITY,
    CONF_FALLBACK,
    KEY_SERVICE,
    KEY_CONDITIONS,
    KEY_MATCH,
)

_LOGGER = logging.getLogger(DOMAIN)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up notify.<slug> from this config entry."""
    data     = entry.data
    slug     = data[CONF_SERVICE_NAME]
    targets  = data[CONF_TARGETS]
    priority = data[CONF_PRIORITY]
    fallback = data[CONF_FALLBACK]

    service = _NotifierService(hass, slug, targets, priority, fallback)
    hass.services.async_register(
        "notify",
        slug,
        service.async_send_message,
        schema=service.SERVICE_SCHEMA
    )
    _LOGGER.debug("Notify platform set up: notify.%s", slug)
    return True


class _NotifierService(BaseNotificationService):
    """Notification service dispatches based on your config."""

    SERVICE_SCHEMA = BaseNotificationService.SERVICE_SCHEMA

    def __init__(self, hass, slug, targets, priority, fallback):
        self.hass      = hass
        self._slug     = slug
        self._targets  = targets
        self._priority = priority
        self._fallback = fallback

    async def async_send_message(self, message="", **kwargs):
        """Evaluate each target in priority; forward or fallback."""
        title = kwargs.get(ATTR_TITLE)
        data  = {ATTR_MESSAGE: message}
        if title:
            data[ATTR_TITLE] = title
        data.update(kwargs.get("data", {}))

        _LOGGER.debug("notify.%s called: title=%s message=%s", self._slug, title, message)
        for svc in self._priority:
            tgt = next((t for t in self._targets if t[KEY_SERVICE] == svc), None)
            if not tgt:
                continue
            mode    = tgt.get(KEY_MATCH, "all")
            _LOGGER.debug("Checking target %s (mode=%s)...", svc, mode)
            results = []
            for cond in tgt[KEY_CONDITIONS]:
                ok = _evaluate_cond(self.hass, cond)
                _LOGGER.debug("  cond %s -> %s", cond, ok)
                results.append(ok)
            matched = all(results) if mode == "all" else any(results)
            _LOGGER.debug("  overall -> %s", matched)
            if matched:
                dom, name = svc.split(".",1)
                _LOGGER.debug("Forwarding to %s.%s", dom, name)
                await self.hass.services.async_call(dom, name, data, blocking=True)
                return

        # fallback
        dom, name = self._fallback.split(".",1)
        _LOGGER.debug("No match; falling back to %s.%s", dom, name)
        await self.hass.services.async_call(dom, name, data, blocking=True)


def _evaluate_cond(hass, cond: dict) -> bool:
    """Return True/False for a single condition."""
    ent = hass.states.get(cond["entity"])
    if not ent:
        return False
    op  = cond["operator"]
    val = cond["value"]

    if val == "unknown or unavailable":
        return ent.state in ("unknown", "unavailable")

    try:
        s = float(ent.state)
        v = float(val)
        if   op == ">":  return s > v
        elif op == "<":  return s < v
        elif op == ">=": return s >= v
        elif op == "<=": return s <= v
    except ValueError:
        pass

    if op == "==": return ent.state == val
    if op == "!=": return ent.state != val
    return False
