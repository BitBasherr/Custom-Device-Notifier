"""Unit-test evaluate_condition helper."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.custom_device_notifier.evaluate import evaluate_condition

pytestmark = pytest.mark.asyncio


async def test_evaluate_condition_numeric(hass: HomeAssistant, enable_custom_integrations: None):
    # Pretend we have a battery sensor at 42 %
    hass.states.async_set("sensor.phone_battery", 42)
    await hass.async_block_till_done()

    cond = {
        "condition": "numeric_state",
        "entity_id": ["sensor.phone_battery"],
        "above": 20,
    }

    assert await evaluate_condition(hass, cond)  # 42 > 20 so True


async def test_evaluate_condition_string(hass: HomeAssistant, enable_custom_integrations: None):
    # Binary-sensor that is currently "on"
    hass.states.async_set("binary_sensor.door", "on")
    await hass.async_block_till_done()

    cond = {
        "condition": "state",
        "entity_id": ["binary_sensor.door"],
        "state": "on",
    }

    assert await evaluate_condition(hass, cond)