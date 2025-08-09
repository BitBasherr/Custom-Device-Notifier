import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.custom_device_notifier.const import (
    CONF_FALLBACK,
    CONF_PRIORITY,
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_MATCH,
    KEY_SERVICE,
)


@pytest.mark.asyncio
async def test_payload_keeps_data_nested_and_forwards_target(hass):
    calls = []

    async def fake_notify(call):
        calls.append(call.data)

    # downstream service we will forward to
    hass.services.async_register("notify", "mobile_app_pixel", fake_notify)

    # config entry with one matching target
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SERVICE_NAME_RAW: "My Notifier",
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
    await hass.async_block_till_done()

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
    await hass.async_block_till_done()

    assert calls, "downstream notify service was not invoked"
    forwarded = calls[0]

    # message/title at root
    assert forwarded.get("message") == "Hello"
    assert forwarded.get("title") == "Title"

    # target preserved
    assert forwarded.get("target") == "device_xyz"

    # rich options remain under 'data'
    assert isinstance(forwarded.get("data"), dict)
    assert forwarded["data"].get("ttl") == 0
    assert forwarded["data"].get("channel") == "alarm_stream"
    assert isinstance(forwarded["data"].get("actions"), list)
    assert forwarded["data"]["actions"][0]["action"] == "URI"

    # nothing leaked to the root
    for k in ("actions", "ttl", "channel"):
        assert k not in forwarded


@pytest.mark.asyncio
async def test_fallback_carries_data(hass):
    calls = []

    async def fallback_call(call):
        calls.append(call.data)

    hass.services.async_register("notify", "fallback_notify", fallback_call)

    # No targets → fallback
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SERVICE_NAME_RAW: "My Notifier",
            CONF_SERVICE_NAME: "my_notifier",
            CONF_TARGETS: [],
            CONF_PRIORITY: [],
            CONF_FALLBACK: "notify.fallback_notify",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        "notify",
        "my_notifier",
        {
            "message": "X",
            "data": {
                "ttl": 0,
                "actions": [{"action": "URI", "title": "Open", "uri": "/"}],
            },
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert calls, "fallback notify service was not invoked"
    forwarded = calls[0]
    assert "data" in forwarded and "actions" in forwarded["data"]
