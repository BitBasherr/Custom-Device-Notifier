"""Test config flow for custom_device_notifier integration."""

import pytest
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant

from custom_components.custom_device_notifier.const import DOMAIN

pytestmark = pytest.mark.asyncio


async def test_user_flow_minimal(hass: HomeAssistant, enable_custom_integrations: None):
    """Walk through the shortest happy path."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    # 1. service name
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"service_name_raw": "Test Notifier"}
    )
    # 2. target service
    assert result["step_id"] == "add_target"