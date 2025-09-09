"""Tests-only shim to register notify.<slug> by delegating to the router.

In production, __init__.py registers the notifier service.
This file exists so tests can import `async_register_services` directly.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, CONF_SERVICE_NAME
from . import _route_and_forward  # ⬅️ IMPORTANT: import the package, not .__init__

_LOGGER = logging.getLogger(DOMAIN)


async def async_register_services(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register notify.<slug> for tests by delegating to _route_and_forward."""
    slug = str(entry.data.get(CONF_SERVICE_NAME) or "").strip()
    if not slug:
        raise ValueError("Missing CONF_SERVICE_NAME in entry.data")

    async def _handle(call: ServiceCall) -> None:
        # Delegate to the same router used in production
        await _route_and_forward(hass, entry, dict(call.data))

    # Avoid duplicate registration in reloaded tests
    if hass.services.has_service("notify", slug):
        hass.services.async_remove("notify", slug)

    hass.services.async_register("notify", slug, _handle)
    _LOGGER.debug("Tests shim registered notify.%s", slug)


# prior notify.py:
# import logging

# from homeassistant.core import HomeAssistant

# from .__init__ import _NotifierService  # re-export the real class
# from .const import DOMAIN

# _LOGGER = logging.getLogger(DOMAIN)


# async def async_register_services(hass: HomeAssistant, entry) -> None:
#    """Register notify.<slug> for tests."""
#    data = entry.data
#    service = _NotifierService(
#        hass,
#        data["service_name"],
#        data["targets"],
#        data["priority"],
#        data["fallback"],
#    )
#    hass.services.async_register(
#        "notify", data["service_name"], service.async_send_message
#    )
