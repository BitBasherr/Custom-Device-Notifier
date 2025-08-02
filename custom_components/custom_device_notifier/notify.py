"""Thin wrapper so tests can import ``async_register_services``.

All logic lives in _NotifierService inside ``__init__.py``.
"""

import logging

from homeassistant.core import HomeAssistant

from .__init__ import _NotifierService  # re-export the real class
from .const import DOMAIN

_LOGGER = logging.getLogger(DOMAIN)


async def async_register_services(hass: HomeAssistant, entry) -> None:
    """Register notify.<slug> for tests."""
    data = entry.data
    service = _NotifierService(
        hass,
        data["service_name"],
        data["targets"],
        data["priority"],
        data["fallback"],
    )
    hass.services.async_register(
        "notify", data["service_name"], service.async_send_message
    )
