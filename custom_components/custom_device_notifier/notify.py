"""Tests shim + payload helper for Custom Device Notifier.

- In production, __init__.py registers notify.<slug> and calls build_notify_payload()
  to normalize the service payload (keep extras nested under data).
- In tests, async_register_services() lets you register notify.<slug> directly and
  still route through the real router.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import CONF_SERVICE_NAME, DOMAIN
from . import _route_and_forward  # import the package, not .__init__

_LOGGER = logging.getLogger(DOMAIN)

# Common mobile_app / alarm_stream / TTS extras we should keep under "data".
# (This set is not strictly required, because we move *all* non-reserved
#  top-level keys into data anyway—but it’s handy to see what we expect.)
KNOWN_DATA_KEYS = {
    # mobile_app core
    "ttl",
    "priority",
    "channel",
    "tag",
    "group",
    "sticky",
    "importance",
    "visibility",
    "color",
    "subject",
    "image",
    "actions",
    "clickAction",
    "notification_icon",
    "ledColor",
    "timeout",
    # alarm stream / media
    "media_stream",
    "media_stream_max",
    "car_ui",
    # TTS
    "tts_text",
    "tts_lang",
    # misc others people often send
    "url",
    "subtitle",
    "icon",
    "sound",
}


def build_notify_payload(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize a notify payload to HA's expected shape without flattening extras.

    Input (raw) may contain:
      - message (str)
      - title (str, optional)
      - target (str | list[str], optional)
      - data (dict, optional)
      - other keys (priority, ttl, channel, media_stream, tts_text, etc.)

    Output:
      {
        "message": <str>,
        "title": <str?>,
        "target": <str|list?>,
        "data": { ... all non-reserved keys merged here ... }
      }
    """
    # Start with existing nested data (if any)
    data = {}
    if isinstance(raw.get("data"), dict):
        data.update(raw["data"])

    # Reserved top-level keys we keep at top level
    reserved_top = {"message", "title", "target", "data"}

    # Move every non-reserved top-level key into data (to avoid flattening).
    # If a key already exists inside data, we do not overwrite it.
    for k, v in raw.items():
        if k in reserved_top:
            continue
        if k not in data:
            data[k] = v

    # Pull core fields
    msg = raw.get("message", "")
    title = raw.get("title")
    target = raw.get("target")

    out: Dict[str, Any] = {"message": msg}
    if title is not None:
        out["title"] = title
    if target is not None:
        out["target"] = target
    if data:
        out["data"] = data
    return out


async def async_register_services(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register notify.<slug> for tests by delegating to the real router."""
    slug = str(entry.data.get(CONF_SERVICE_NAME) or "").strip()
    if not slug:
        raise ValueError("Missing CONF_SERVICE_NAME in entry.data")

    async def _handle(call: ServiceCall) -> None:
        await _route_and_forward(hass, entry, dict(call.data))

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
