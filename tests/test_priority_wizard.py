from homeassistant import data_entry_flow

DOMAIN = "custom_device_notifier"


async def test_full_config_flow(hass):
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
    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["step_id"] == "user"

    # Name
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "My Notifier"}
    )
    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["step_id"] == "add_target"

    # Add target
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "mobile_app_pixel"}
    )
    assert result["step_id"] == "condition_more"

    # Add condition -> entity
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "add"}
    )
    assert result["step_id"] == "add_condition_entity"

    # Pick entity
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity": "sensor.pixel_battery"}
    )
    assert result["step_id"] == "add_condition_value"

    # Operator/value (numeric)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator": ">", "value_choice": "manual", "value": 20.0}
    )
    assert result["step_id"] == "condition_more"

    # Done -> match mode
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    assert result["step_id"] == "match_mode"

    # Require all
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    assert result["step_id"] == "target_more"

    # Done with targets -> order
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "done"}
    )
    assert result["step_id"] == "order_targets"

    # Keep default order -> fallback
    # Provide list exactly as returned by the form's default
    priority = ["notify.mobile_app_pixel"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": priority}
    )
    assert result["step_id"] == "choose_fallback"

    # Choose fallback and finish
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "mobile_app_pixel"}
    )
    assert result["type"] == data_entry_flow.RESULT_TYPE_CREATE_ENTRY
    assert result["title"] == "My Notifier"
    data = result["data"]
    assert data["targets"][0]["service"] == "notify.mobile_app_pixel"


async def test_back_from_fallback(hass):
    hass.services.async_register("notify", "mobile_app_pixel", lambda call: None)
    hass.states.async_set("sensor.pixel_battery", "55")

    res = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"service_name_raw": "My Notifier"}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"target_service": "mobile_app_pixel"}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"choice": "done"}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"match_mode": "all"}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"next": "done"}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"priority": ["notify.mobile_app_pixel"]}
    )
    assert res["step_id"] == "choose_fallback"

    # Back (via nav selector)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"fallback": "mobile_app_pixel", "nav": "back"}
    )
    assert res["step_id"] == "order_targets"
