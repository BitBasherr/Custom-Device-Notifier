"""Custom Device Notifier integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

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

_LOGGER: Final = logging.getLogger(DOMAIN)

# ---------- service schema -------------------------------------------------

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_TITLE): cv.string,
        vol.Optional("data"): dict,
    }
)

# ---------- setup ----------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Create the notify service from a ConfigEntry."""
    try:
        data = entry.data
        slug: str = data[CONF_SERVICE_NAME]
        targets: list[dict[str, Any]] = data[CONF_TARGETS]
        priority: list[str] = data[CONF_PRIORITY]
        fallback: str = data[CONF_FALLBACK]

        service = _NotifierService(hass, slug, targets, priority, fallback)

        hass.services.async_register(
            "notify", slug, service.async_send_message, schema=SERVICE_SCHEMA
        )

        # Forward any companion platforms (e.g. sensor/)
        await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
        return True

    except Exception:  # pragma: no cover – log & signal failure
        _LOGGER.exception("Error setting up %s", DOMAIN)
        return False


# ---------- service implementation ----------------------------------------


class _NotifierService(BaseNotificationService):
    """Forward a message to the first target whose conditions match."""

    def __init__(
        self,
        hass: HomeAssistant,
        slug: str,
        targets: list[dict[str, Any]],
        priority: list[str],
        fallback: str,
    ) -> None:
        self.hass = hass
        self._slug = slug
        self._targets = targets
        self._priority = priority
        self._fallback = fallback

    # ---------------------------------------------------------------------

    async def async_send_message(self, message: str = "", **kwargs: Any) -> None:
        """Dispatch *message* to the first matching target (or fallback)."""
        title = kwargs.get(ATTR_TITLE)
        data: dict[str, Any] = {ATTR_MESSAGE: message}
        if title:
            data[ATTR_TITLE] = title
        data.update(kwargs.get("data", {}))

        _LOGGER.debug("notify.%s called: %s / %s", self._slug, title, message)

        # — iterate through priority list —
        for svc in self._priority:
            tgt = next((t for t in self._targets if t[KEY_SERVICE] == svc), None)
            if not tgt:
                continue

            mode = tgt.get(KEY_MATCH, "all")

            # Evaluate all conditions concurrently
            results = await asyncio.gather(
                *(evaluate_condition(self.hass, c) for c in tgt[KEY_CONDITIONS])
            )
            matched = all(results) if mode == "all" else any(results)
            _LOGGER.debug("  target %s match=%s", svc, matched)

            if matched:
                dom, name = svc.split(".", 1)
                _LOGGER.debug("  → forwarding to %s.%s", dom, name)
                await self.hass.services.async_call(dom, name, data, blocking=True)
                return  # stop after first successful target

        # Nothing matched ⇒ fallback
        dom, name = self._fallback.split(".", 1)
        _LOGGER.debug("  → fallback to %s.%s", dom, name)
        await self.hass.services.async_call(dom, name, data, blocking=True)