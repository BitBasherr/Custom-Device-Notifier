import pytest
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType

from custom_components.custom_device_notifier.const import DOMAIN
from custom_components.custom_device_notifier import config_flow


@pytest.mark.asyncio
async def test_minimal_config_flow(hass: HomeAssistant):
    flow = config_flow.CustomDeviceNotifierConfigFlow()
    flow.hass = hass

    # Step 1: Start flow with a valid name
    result = await flow.async_step_user({"service_name_raw": "Test Notifier"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "add_target"

    # Step 2: Add target service
    result = await flow.async_step_add_target({"target_service": "notify.test_service"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "add_condition"

    # Step 3: Add condition (mocked entity)
    ent_id = "sensor.test_value"
    hass.states.async_set(ent_id, "42")
    result = await flow.async_step_add_condition({"entity": ent_id})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "match_mode"

    # Step 4: Select match mode
    result = await flow.async_step_match_mode({"match_mode": "all"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "condition_more"

    # Step 5: Done with conditions
    result = await flow.async_step_condition_more({"choice": "done"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "target_more"

    # Step 6: Done with targets
    result = await flow.async_step_target_more({"next": "done"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "order_targets"

    # Step 7: Order targets
    result = await flow.async_step_order_targets({"priority": ["notify.test_service"]})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "choose_fallback"

    # Step 8: Fallback
    result = await flow.async_step_choose_fallback({"fallback": "notify.test_service"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test Notifier"
    assert result["data"]["service_name"] == "test_notifier"
