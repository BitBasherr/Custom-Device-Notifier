import pytest
from homeassistant.core import HomeAssistant

from custom_components.custom_device_notifier.const import DOMAIN


@pytest.mark.asyncio
async def test_full_config_flow(hass: HomeAssistant):
    # Fake a notify service & an entity
    called = []

    async def _noop(call):
        called.append(call)

    hass.services.async_register("notify", "mobile_app_pixel", _noop)
    hass.states.async_set("sensor.pixel_battery", "55")

    # Start
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    # RESULT_TYPE_FORM constants were removed; compare to string
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    # name
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "My Notifier"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_target"

    # target
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "mobile_app_pixel"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "condition_more"

    # done with conditions right away
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "match_mode"

    # all conditions (irrelevant here)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "target_more"

    # done with targets
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "done"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "order_targets"

    # priority (single entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": ["notify.mobile_app_pixel"]}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "choose_fallback"

    # choose fallback and continue to create entry
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "mobile_app_pixel", "nav": "continue"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "My Notifier"
    data = result["data"]
    assert data["fallback"] == "notify.mobile_app_pixel"
    assert data["service_name"] == "my_notifier"
    assert data["targets"][0]["service"] == "notify.mobile_app_pixel"


@pytest.mark.asyncio
async def test_back_from_fallback(hass: HomeAssistant):
    hass.services.async_register("notify", "mobile_app_pixel", lambda call: None)
    hass.states.async_set("sensor.pixel_battery", "55")

    res = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert res["type"] == "form"
    assert res["step_id"] == "user"

    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"service_name_raw": "My Notifier"}
    )
    assert res["type"] == "form"
    assert res["step_id"] == "add_target"

    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"target_service": "mobile_app_pixel"}
    )
    assert res["type"] == "form"
    assert res["step_id"] == "condition_more"

    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"choice": "done"}
    )
    assert res["type"] == "form"
    assert res["step_id"] == "match_mode"

    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"match_mode": "all"}
    )
    assert res["type"] == "form"
    assert res["step_id"] == "target_more"

    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"next": "done"}
    )
    assert res["type"] == "form"
    assert res["step_id"] == "order_targets"

    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"priority": ["notify.mobile_app_pixel"]}
    )
    assert res["type"] == "form"
    assert res["step_id"] == "choose_fallback"

    # Back (via nav selector) — this used to fail because 'nav' wasn't in schema
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"fallback": "mobile_app_pixel", "nav": "back"}
    )
    assert res["type"] == "form"
    assert res["step_id"] == "order_targets"
