"""Custom Device Notifier integration (dynamic notify + sensor)."""

import logging
import voluptuous as vol

import logging
from .const import DOMAIN
_LOGGER = logging.getLogger(DOMAIN)

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MESSAGE, CONF_TITLE
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    CONF_PRIORITY,
    CONF_FALLBACK,
    KEY_SERVICE,
    KEY_CONDITIONS,
    KEY_MATCH,
)

SERVICE_SCHEMA = vol.Schema({
    vol.Required(CONF_MESSAGE): cv.string,
    vol.Optional(CONF_TITLE):   cv.string,
    vol.Optional("data"):       dict
}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Nothing at core startup."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate v1 → v2 (seed missing fallback)."""
    if entry.version == 1:
        data = dict(entry.data)
        targets = data.get(CONF_TARGETS, [])
        if data.get(CONF_FALLBACK) is None and targets:
            data[CONF_FALLBACK] = targets[0][KEY_SERVICE]
            _LOGGER.debug("Migrated: seeded fallback=%s", data[CONF_FALLBACK])
        entry.data = data
        entry.version = 2
        _LOGGER.debug("Migrated Custom Device Notifier to version 2")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register notify service, dev-tool, and sensor platform."""
    data     = entry.data
    slug     = data[CONF_SERVICE_NAME]
    raw_name = data.get(CONF_SERVICE_NAME_RAW, slug)
    targets  = data[CONF_TARGETS]
    priority = data[CONF_PRIORITY]
    fallback = data[CONF_FALLBACK]

    async def _notify(call):
        msg   = call.data.get(CONF_MESSAGE)
        title = call.data.get(CONF_TITLE)
        extra = call.data.get("data", {})

        _LOGGER.debug("notify.%s called: title=%s msg=%s", slug, title, msg)
        for svc_id in priority:
            tgt = next((t for t in targets if t[KEY_SERVICE] == svc_id), None)
            if not tgt:
                continue
            mode = tgt.get(KEY_MATCH, "all")
            _LOGGER.debug("  Checking %s (mode=%s)", svc_id, mode)
            results = [_evaluate_cond(hass, c) for c in tgt[KEY_CONDITIONS]]
            _LOGGER.debug("    condition results: %s", results)
            matched = all(results) if mode == "all" else any(results)
            if matched:
                dom, svc = svc_id.split(".", 1)
                _LOGGER.debug("  → Forwarding to %s.%s", dom, svc)
                await hass.services.async_call(
                    dom, svc,
                    {**extra, CONF_MESSAGE: msg, **({CONF_TITLE: title} if title else {})},
                    blocking=True,
                )
                return

        dom, svc = fallback.split(".", 1)
        _LOGGER.debug("  → Falling back to %s.%s", dom, svc)
        await hass.services.async_call(
            dom, svc,
            {**extra, CONF_MESSAGE: msg, **({CONF_TITLE: title} if title else {})},
            blocking=True,
        )

    hass.services.async_register("notify", slug, _notify, schema=SERVICE_SCHEMA)
    _LOGGER.debug("Registered notify.%s", slug)

    async def _evaluate(call):
        """Dev-tool: dump current eval state."""
        _LOGGER.debug("Dev-evaluate for notify.%s", slug)
        for tgt in targets:
            svc_id = tgt[KEY_SERVICE]
            mode   = tgt.get(KEY_MATCH, "all")
            _LOGGER.debug("  Target %s (mode=%s):", svc_id, mode)
            res = []
            for cond in tgt[KEY_CONDITIONS]:
                ok = _evaluate_cond(hass, cond)
                _LOGGER.debug("    %s -> %s", cond, ok)
                res.append(ok)
            _LOGGER.debug("    overall -> %s", all(res) if mode == "all" else any(res))

    hass.services.async_register(DOMAIN, "evaluate", _evaluate)
    _LOGGER.debug("Registered %s.evaluate", DOMAIN)

    # forward to sensor
    await hass.config_entries.async_forward_entry_setup(entry, "sensor")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload notify, evaluate and sensor."""
    slug = entry.data[CONF_SERVICE_NAME]
    hass.services.async_remove("notify", slug)
    hass.services.async_remove(DOMAIN, "evaluate")
    await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    _LOGGER.debug("Unloaded notify.%s, %s.evaluate, sensor", slug, DOMAIN)
    return True


def _evaluate_cond(hass, cond: dict) -> bool:
    """Evaluate one condition (entity, op, value)."""
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
        if   op == "<":  return s < v
        if   op == ">=": return s >= v
        if   op == "<=": return s <= v
    except ValueError:
        pass

    if op == "==": return ent.state == val
    if op == "!=": return ent.state != val
    return False
