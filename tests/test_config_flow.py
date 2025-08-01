"""Test config flow for custom_device_notifier integration."""

import pytest
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant

from custom_components.custom_device_notifier.const import DOMAIN

pytestmark = pytest.mark.asyncio


async def test_user_flow(hass: HomeAssistant):
    """Test the initial user step."""
    # Start config flow
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    assert result["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result["step_id"] == "user"

    # Provide user input
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"service_name_raw": "Test Notifier"}
    )

    assert result2["type"] == data_entry_flow.RESULT_TYPE_FORM
    assert result2["step_id"] == "add_target"
