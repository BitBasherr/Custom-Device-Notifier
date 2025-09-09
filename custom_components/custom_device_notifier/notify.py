from __future__ import annotations

from typing import Any, Dict, Mapping


def build_notify_payload(payload: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    """
    Normalize a Home Assistant notify payload.

    - Keeps nested 'data' intact
    - Moves unknown top-level extras into data[*] (without overwriting existing keys)
    - Returns only HA-notify compliant keys: message, title, target, data
    """
    src: Dict[str, Any] = dict(payload or {})

    message = src.pop("message", None)
    title = src.pop("title", None)
    target = src.pop("target", None)
    data = src.pop("data", None)

    # Ensure 'data' is a dict if present
    if data is None:
        data = {}
    elif not isinstance(data, dict):
        data = {"value": data}

    # Move remaining unknown keys under data without clobbering
    for k, v in list(src.items()):
        if k in ("message", "title", "target", "data"):
            continue
        if k not in data:
            data[k] = v

    out: Dict[str, Any] = {}
    if message is not None:
        out["message"] = message
    if title is not None:
        out["title"] = title
    if target is not None:
        out["target"] = target
    if data:
        out["data"] = data

    return out

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
