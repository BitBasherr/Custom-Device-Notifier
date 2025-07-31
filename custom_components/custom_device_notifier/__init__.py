"""Custom Device Notifier integration."""
import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.notify import ATTR_MESSAGE, ATTR_TITLE

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

_LOGGER = logging.getLogger(DOMAIN)

SERVICE_SCHEMA = {
    # will be applied in each notify.<slug> registration
}


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Nothing at startup."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up notify.<slug>, developer evaluate, and sensor."""
    data     = entry.data
    slug     = data[CONF_SERVICE_NAME]
    raw      = data[CONF_SERVICE_NAME_RAW]
    targets  = data[CONF_TARGETS]
    priority = data[CONF_PRIORITY]
    fallback = data[CONF_FALLBACK]

    async def _notify(call):
        """Handle service call to notify.<slug>."""
        msg   = call.data.get(ATTR_MESSAGE)
        title = call.data.get(ATTR_TITLE)
        extra = call.data.get("data", {})

        _LOGGER.debug("notify.%s called: title=%s msg=%s", slug, title, msg)
        for svc_id in priority:
            tgt = next((t for t in targets if t[KEY_SERVICE] == svc_id), None)
            if not tgt:
                continue
            mode = tgt.get(KEY_MATCH, "all")
            results = [_evaluate_cond(hass, c) for c in tgt[KEY_CONDITIONS]]
            matched = all(results) if mode == "all" else any(results)
            _LOGGER.debug("  %s match=%s results=%s", svc_id, matched, results)
            if matched:
                dom, svc = svc_id.split(".", 1)
                _LOGGER.debug("  → Forwarding to %s.%s", dom, svc)
                await hass.services.async_call(
                    dom, svc,
                    {**extra, ATTR_MESSAGE: msg, **({ATTR_TITLE: title} if title else {})},
                    blocking=True,
                )
                return

        # fallback
        dom, svc = fallback.split(".", 1)
        _LOGGER.debug("  → Falling back to %s.%s", dom, svc)
        await hass.services.async_call(
            dom, svc,
            {**extra, ATTR_MESSAGE: msg, **({ATTR_TITLE: title} if title else {})},
            blocking=True,
        )

    hass.services.async_register("notify", slug, _notify)

    async def _evaluate(call):
        """Developer tool: log each condition result."""
        _LOGGER.debug("evaluate called for notify.%s", slug)
        for tgt in targets:
            svc_id = tgt[KEY_SERVICE]
            mode   = tgt.get(KEY_MATCH, "all")
            res = [_evaluate_cond(hass, c) for c in tgt[KEY_CONDITIONS]]
            overall = all(res) if mode == "all" else any(res)
            _LOGGER.debug("%s mode=%s results=%s overall=%s", svc_id, mode, res, overall)

    hass.services.async_register(DOMAIN, "evaluate", _evaluate)
    _LOGGER.debug("Registered notify.%s and %s.evaluate", slug, DOMAIN)

    await hass.config_entries.async_forward_entry_setup(entry, "sensor")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload notify, evaluate, and sensor."""
    data = entry.data
    slug = data[CONF_SERVICE_NAME]
    hass.services.async_remove("notify", slug)
    hass.services.async_remove(DOMAIN, "evaluate")
    await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    _LOGGER.debug("Unloaded notify.%s and sensor", slug)
    return True


def _evaluate_cond(hass, cond: dict) -> bool:
    """Evaluate a single condition dict."""
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
        if op == ">":  return s > v
        if op == "<":  return s < v
        if op == ">=": return s >= v
        if op == "<=": return s <= v
    except ValueError:
        pass

    if op == "==": return ent.state == val
    if op == "!=": return ent.state != val
    return False
