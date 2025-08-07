"""Test config flow for custom_device_notifier integration."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.custom_device_notifier.const import (
    CONF_FALLBACK,
    CONF_TARGETS,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_SERVICE,
)

pytestmark = pytest.mark.asyncio


async def test_user_flow_minimal(hass: HomeAssistant, enable_custom_integrations: None):
    """Test minimal config flow with single target and one condition."""
    hass.services.async_register("notify", "test_notify", lambda msg: None)
    hass.services.async_register("notify", "fallback_notify", lambda msg: None)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Test Notifier"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_target"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "test_notify"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "condition_more"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "add"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_condition_entity"

    hass.states.async_set("sensor.test_battery", 50)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity": "sensor.test_battery"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_condition_value"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator": ">", "value": 40, "value_choice": "current"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "condition_more"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "match_mode"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "target_more"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "done"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "order_targets"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": ["notify.test_notify"]}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "choose_fallback"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Test Notifier"
    assert result["data"]["service_name_raw"] == "Test Notifier"
    assert result["data"][CONF_TARGETS][0][KEY_SERVICE] == "notify.test_notify"
    assert (
        result["data"][CONF_TARGETS][0][KEY_CONDITIONS][0]["entity_id"]
        == "sensor.test_battery"
    )
    assert result["data"][CONF_TARGETS][0][KEY_CONDITIONS][0]["value"] == "40"
    assert result["data"][CONF_FALLBACK] == "notify.fallback_notify"


async def test_user_flow_multiple_targets(
    hass: HomeAssistant, enable_custom_integrations: None
):
    """Test config flow with multiple targets and conditions."""
    hass.services.async_register("notify", "phone_notify", lambda msg: None)
    hass.services.async_register("notify", "tablet_notify", lambda msg: None)
    hass.services.async_register("notify", "fallback_notify", lambda msg: None)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Multi Notifier"}
    )

    # First target
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "phone_notify"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "add"}
    )
    hass.states.async_set("binary_sensor.door", "on")
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity": "binary_sensor.door"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator": "==", "value": "on", "value_choice": "current"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "any"}
    )

    # Second target
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "add"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "tablet_notify"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "add"}
    )
    hass.states.async_set("sensor.test_battery", 50)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity": "sensor.test_battery"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator": ">", "value": 40, "value_choice": "current"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )

    # Complete setup
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": ["notify.phone_notify", "notify.tablet_notify"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )

    assert result["type"] == "create_entry"
    assert len(result["data"][CONF_TARGETS]) == 2
    assert result["data"][CONF_TARGETS][0][KEY_SERVICE] == "notify.phone_notify"
    assert (
        result["data"][CONF_TARGETS][0][KEY_CONDITIONS][0]["entity_id"]
        == "binary_sensor.door"
    )
    assert result["data"][CONF_TARGETS][0][KEY_CONDITIONS][0]["value"] == "on"
    assert result["data"][CONF_TARGETS][1][KEY_SERVICE] == "notify.tablet_notify"
    assert (
        result["data"][CONF_TARGETS][1][KEY_CONDITIONS][0]["entity_id"]
        == "sensor.test_battery"
    )
    assert result["data"][CONF_TARGETS][1][KEY_CONDITIONS][0]["value"] == "40"
    assert result["data"][CONF_FALLBACK] == "notify.fallback_notify"


async def test_add_target_error_invalid_service(
    hass: HomeAssistant, enable_custom_integrations: None
):
    """Test error when submitting invalid target service."""
    hass.services.async_register("notify", "test_notify", lambda msg: None)
    hass.services.async_register("notify", "fallback_notify", lambda msg: None)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Error Test"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "invalid_service"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "add_target"
    assert result["errors"]["target_service"] == "must_be_notify"


async def test_condition_removal(hass: HomeAssistant, enable_custom_integrations: None):
    """Test removing a condition from a target."""
    hass.services.async_register("notify", "test_notify", lambda msg: None)
    hass.services.async_register("notify", "fallback_notify", lambda msg: None)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Remove Test"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "test_notify"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "add"}
    )
    hass.states.async_set("sensor.test_battery", 50)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity": "sensor.test_battery"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator": ">", "value": 40, "value_choice": "current"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "remove"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"conditions_to_remove": ["sensor.test_battery > 40"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": ["notify.test_notify"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )

    assert result["type"] == "create_entry"
    assert len(result["data"][CONF_TARGETS][0][KEY_CONDITIONS]) == 0


async def test_condition_edit(hass: HomeAssistant, enable_custom_integrations: None):
    """Test editing a condition in a target."""
    hass.services.async_register("notify", "test_notify", lambda msg: None)
    hass.services.async_register("notify", "fallback_notify", lambda msg: None)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Edit Test"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "test_notify"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "add"}
    )
    hass.states.async_set("sensor.test_battery", 50)
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entity": "sensor.test_battery"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator": ">", "value": 40, "value_choice": "current"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "edit"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"condition": "sensor.test_battery > 40"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator": "<", "value": 60, "value_choice": "current"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": ["notify.test_notify"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )

    assert result["type"] == "create_entry"
    assert (
        result["data"][CONF_TARGETS][0][KEY_CONDITIONS][0]["entity_id"]
        == "sensor.test_battery"
    )
    assert result["data"][CONF_TARGETS][0][KEY_CONDITIONS][0]["operator"] == "<"
    assert result["data"][CONF_TARGETS][0][KEY_CONDITIONS][0]["value"] == "60"


async def test_target_edit_and_retention(
    hass: HomeAssistant, enable_custom_integrations: None
):
    """Test editing a target and verifying retention in options flow."""
    hass.services.async_register("notify", "phone_notify", lambda msg: None)
    hass.services.async_register("notify", "tablet_notify", lambda msg: None)
    hass.services.async_register("notify", "fallback_notify", lambda msg: None)

    # Initial setup with two targets
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Retention Test"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "phone_notify"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "add"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "tablet_notify"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": ["notify.phone_notify", "notify.tablet_notify"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )
    assert result["type"] == "create_entry"
    assert len(result["data"][CONF_TARGETS]) == 2

    # Edit: Add a condition to the first target
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next": "edit"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"target": "notify.phone_notify"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"choice": "add"}
    )
    hass.states.async_set("sensor.test_battery", 50)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity": "sensor.test_battery"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"operator": ">", "value": 40, "value_choice": "current"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next": "done"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"priority": ["notify.phone_notify", "notify.tablet_notify"]}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )
    assert result["type"] == "create_entry"
    assert len(result["data"][CONF_TARGETS]) == 2
    assert (
        result["data"][CONF_TARGETS][0][KEY_CONDITIONS][0]["entity_id"]
        == "sensor.test_battery"
    )
    assert result["data"][CONF_TARGETS][0][KEY_CONDITIONS][0]["value"] == "40"

    # Verify retention in new options flow
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    assert result["step_id"] == "target_more"


async def test_target_removal(hass: HomeAssistant, enable_custom_integrations: None):
    """Test removing a target."""
    hass.services.async_register("notify", "phone_notify", lambda msg: None)
    hass.services.async_register("notify", "tablet_notify", lambda msg: None)
    hass.services.async_register("notify", "fallback_notify", lambda msg: None)

    # Initial setup with two targets
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Remove Target Test"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "phone_notify"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "add"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"target_service": "tablet_notify"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"choice": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"match_mode": "all"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next": "done"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"priority": ["notify.phone_notify", "notify.tablet_notify"]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )
    assert result["type"] == "create_entry"
    assert len(result["data"][CONF_TARGETS]) == 2

    # Remove one target
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next": "remove"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"targets": ["notify.tablet_notify"]}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next": "done"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"priority": ["notify.phone_notify"]}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"fallback": "fallback_notify"}
    )
    assert result["type"] == "create_entry"
    assert len(result["data"][CONF_TARGETS]) == 1
    assert result["data"][CONF_TARGETS][0][KEY_SERVICE] == "notify.phone_notify"
