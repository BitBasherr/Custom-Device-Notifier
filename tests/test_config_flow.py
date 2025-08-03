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

    # Skip conditions by going to condition_more and choosing "done" (but since no condition added, simulate direct to match_mode if logic allows; adjust based on flow)
    # Note: If flow requires at least one condition, add minimal one
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
    assert result["data"]["service_name"] == "test_notifier"  # slugified
    assert "targets" in result["data"]
    assert len(result["data"]["targets"]) == 1
    assert result["data"]["targets"][0]["service"] == "notify.test_notify"
    assert result["data"]["targets"][0]["conditions"] == [
        {"entity_id": "sensor.test_battery", "operator": ">", "value": 40}
    ]
    assert result["data"]["targets"][0]["match"] == "all"
    assert result["data"]["priority"] == ["notify.test_notify"]
    assert result["data"]["fallback"] == "notify.fallback_notify"


async def test_user_flow_with_multiple_targets(
    hass: HomeAssistant, enable_custom_integrations: None
):
    """Test config flow with multiple targets and conditions."""
    # Mock services
    hass.services.async_register("notify", "primary_notify", lambda msg: None)
    hass.services.async_register("notify", "secondary_notify", lambda msg: None)
    hass.services.async_register("notify", "fallback_notify", lambda msg: None)

    # Mock entities
    hass.states.async_set("binary_sensor.door", "on")
    hass.states.async_set("sensor.battery", 30)
    await hass.async_block_till_done()

    # Initiate
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})

    # Service name
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Multi Target Notifier"}
    )

    # Add first target
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "primary_notify"}
    )

    # Add condition for first target
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity": "binary_sensor.door"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator": "==", "value": "on"}
    )

    # More conditions? Done
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": "done"})

    # Match mode
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "any"}
    )

    # More targets? Add another
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next": "add"})

    # Add second target
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "secondary_notify"}
    )

    # Add condition for second target
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity": "sensor.battery"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator": "<", "value": 50}
    )

    # Done conditions
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": "done"})

    # Match mode for second
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )

    # No more targets
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next": "done"})

    # Priority order
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": ["notify.primary_notify", "notify.secondary_notify"]}
    )

    # Fallback
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )

    assert result["type"] == "create_entry"
    assert len(result["data"]["targets"]) == 2
    # Add more assertions as needed


async def test_add_target_error_invalid_service(
    hass: HomeAssistant, enable_custom_integrations: None
):
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
    assert "must_be_notify" in result["errors"]


# Add similar tests for other errors, like no targets, invalid fallback, unique ID abort, etc.
