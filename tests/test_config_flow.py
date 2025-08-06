"""Test config flow for custom_device_notifier integration."""

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.custom_device_notifier.const import (
    CONF_FALLBACK,
    CONF_PRIORITY,
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    DOMAIN,
    STEP_TARGET_MORE,
)

pytestmark = pytest.mark.asyncio


async def test_user_flow_minimal(hass: HomeAssistant, enable_custom_integrations: None):
    """Walk through a simulated full config flow with minimal inputs (single target, one condition)."""
    # Mock a notify service
    hass.services.async_register("notify", "test_notify", lambda msg: None)
    hass.services.async_register("notify", "fallback_notify", lambda msg: None)
    hass.services.async_register("notify", "persistent_notification", lambda msg: None)

    # Initiate flow
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert not result["errors"]

    # Step 1: Submit service name
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Test Notifier"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_target"
    assert not result["errors"]

    # Step 2: Submit target service
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "test_notify"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "condition_more"
    # Submit to add condition
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "add"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_condition_entity"

    # Step 3: Submit entity for condition
    hass.states.async_set("sensor.test_battery", 50)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity": "sensor.test_battery"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_condition_value"

    # Step 4: Submit condition value
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator": ">", "value": 40}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "condition_more"

    # Step 5: Done with conditions
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "match_mode"

    # Step 6: Submit match mode
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "target_more"

    # Step 7: Done with targets (single target)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "done"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "order_targets"

    # Step 8: Submit priority order
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": ["notify.test_notify"]}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "choose_fallback"

    # Step 9: Submit fallback
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Test Notifier"
    assert result["data"]["service_name_raw"] == "Test Notifier"
    assert "targets" in result["data"]
    assert result["data"]["fallback"] == "notify.fallback_notify"


async def test_add_target_error_invalid_service(
    hass: HomeAssistant, enable_custom_integrations: None
):
    """Test error when submitting invalid target service."""
    # Initiate flow
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    # Submit name
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Error Test"}
    )

    # Submit invalid target
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "invalid_service"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_target"
    assert result["errors"]["target_service"] == "must_be_notify"


async def test_options_flow(hass: HomeAssistant, enable_custom_integrations: None):
    """Test the options flow for Custom Device Notifier."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Notifier",
        data={
            CONF_SERVICE_NAME: "test_notifier",
            CONF_SERVICE_NAME_RAW: "Test Notifier",
            CONF_TARGETS: [{"service": "notify.mobile_app", "conditions": []}],
            CONF_PRIORITY: ["notify.mobile_app"],
            CONF_FALLBACK: "notify.persistent_notification",
        },
        entry_id="test_entry_id",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Initiate options flow
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["step_id"] == STEP_TARGET_MORE


async def test_options_flow_reuses_existing_config(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.services.async_register("notify", "mobile_app", lambda msg: None)
    hass.services.async_register("notify", "persistent_notification", lambda msg: None)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SERVICE_NAME: "test_notifier",
            CONF_SERVICE_NAME_RAW: "Test Notifier",
            CONF_TARGETS: [{"service": "notify.mobile_app", "conditions": []}],
            CONF_PRIORITY: ["notify.mobile_app"],
            CONF_FALLBACK: "notify.persistent_notification",
        },
    )
    entry.add_to_hass(hass)

    # Load entry into hass
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Start options flow
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    assert result["step_id"] == STEP_TARGET_MORE
    assert "flow_id" in result, f"Flow failed unexpectedly: {result}"
    flow_id = result["flow_id"]  # ✅ Save flow_id here

    # Continue through options flow to validate prepopulated data
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next": "done"}
    )
    assert result["step_id"] == "order_targets"

    result = await hass.config_entries.options.async_configure(
        flow_id, {"priority": ["notify.mobile_app"]}
    )
    assert result["step_id"] == "choose_fallback"

    result = await hass.config_entries.options.async_configure(
        flow_id, {"fallback": "persistent_notification"}
    )
    assert result["type"] == "create_entry"
    assert entry.data[CONF_FALLBACK] == "notify.persistent_notification"
