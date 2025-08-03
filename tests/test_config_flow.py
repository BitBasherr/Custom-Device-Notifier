"""Test config flow for custom_device_notifier integration."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.custom_device_notifier.const import DOMAIN

pytestmark = pytest.mark.asyncio


async def test_user_flow_minimal(hass: HomeAssistant, enable_custom_integrations: None):
    """Walk through a simulated full config flow with minimal inputs (no conditions, single target)."""
    # Mock a notify service
    hass.services.async_register("notify", "test_notify", lambda msg: None)
    hass.services.async_register("notify", "fallback_notify", lambda msg: None)

    # Initiate flow
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
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
    assert result["step_id"] == "add_condition_entity"
    assert not result["errors"]

    # Submit entity for condition
    hass.states.async_set("sensor.test_battery", 50)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity": "sensor.test_battery"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_condition_value"

    # Submit condition value
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator": ">", "value": 40}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "condition_more"

    # Done with conditions
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": "done"})
    assert result["type"] == "form"
    assert result["step_id"] == "match_mode"

    # Submit match mode
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "target_more"

    # Done with targets (single target)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next": "done"})
    assert result["type"] == "form"
    assert result["step_id"] == "order_targets"

    # Submit priority order
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": ["notify.test_notify"]}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "choose_fallback"

    # Submit fallback
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Test Notifier"
    assert result["data"]["service_name_raw"] == "Test Notifier"
    assert "targets" in result["data"]
    assert result["data"]["fallback"] == "notify.fallback_notify"


async def test_add_target_error_invalid_service(hass: HomeAssistant, enable_custom_integrations: None):
    """Test error when submitting invalid target service."""
    # Initiate and submit name
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
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