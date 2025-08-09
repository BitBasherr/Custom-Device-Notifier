# tests/test_notify_payload.py
import pytest
from homeassistant.core import HomeAssistant

from custom_components.custom_device_notifier.const import (
    CONF_FALLBACK,
    CONF_PRIORITY,
    CONF_SERVICE_NAME,
    CONF_TARGETS,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_MATCH,
    KEY_SERVICE,
)
from tests.common import MockConfigEntry


@pytest.mark.asyncio
async def test_payload_keeps_data_nested_and_forwards_target(hass: HomeAssistant):
    calls = []

    async def fake_notify(call):
        calls.append(call.data)

    # downstream service we will forward to
    hass.services.async_register("notify", "mobile_app_pixel", fake_notify)

    # integration config: one target, no conditions, priority hits it
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SERVICE_NAME: "my_notifier",
            CONF_TARGETS: [
                {
                    KEY_SERVICE: "notify.mobile_app_pixel",
                    KEY_CONDITIONS: [],
                    KEY_MATCH: "all",
                }
            ],
            CONF_PRIORITY: ["notify.mobile_app_pixel"],
            CONF_FALLBACK: "notify.mobile_app_pixel",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    # call our notifier with rich data + target
    await hass.services.async_call(
        "notify",
        "my_notifier",
        {
            "message": "Hello",
            "title": "Title",
            "target": "device_xyz",
            "data": {
                "actions": [{"action": "URI", "title": "Open", "uri": "/path"}],
                "ttl": 0,
                "channel": "alarm_stream",
            },
        },
        blocking=True,
    )

    assert calls, "downstream notify service was not invoked"
    forwarded = calls[0]

    # The important bits:
    assert forwarded.get("message") == "Hello"
    assert forwarded.get("title") == "Title"

    # target must be preserved
    assert forwarded.get("target") == "device_xyz"

    # rich options must remain nested under 'data'
    assert "data" in forwarded and isinstance(forwarded["data"], dict)
    assert forwarded["data"].get("ttl") == 0
    assert forwarded["data"].get("channel") == "alarm_stream"
    assert isinstance(forwarded["data"].get("actions"), list)
    assert forwarded["data"]["actions"][0]["action"] == "URI"

    # and nothing leaked to the root:
    assert "actions" not in forwarded
    assert "ttl" not in forwarded
    assert "channel" not in forwarded


@pytest.mark.asyncio
async def test_fallback_carries_data(hass: HomeAssistant):
    calls = []

    async def fb(call):
        calls.append(call.data)

    hass.services.async_register("notify", "fallback_notify", fb)

    # No matching targets → fallback
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SERVICE_NAME: "my_notifier",
            CONF_TARGETS: [],  # nothing to match
            CONF_PRIORITY: [],
            CONF_FALLBACK: "notify.fallback_notify",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    await hass.services.async_call(
        "notify",
        "my_notifier",
        {
            "message": "X",
            "data": {"ttl": 0, "actions": [{"action": "URI", "uri": "/"}]},
        },
        blocking=True,
    )
    assert calls and "data" in calls[0] and "actions" in calls[0]["data"]
