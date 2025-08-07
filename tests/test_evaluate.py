"""Unit-test evaluate_condition helper."""

import pytest
from datetime import time as dt_time
from homeassistant.core import HomeAssistant

from custom_components.custom_device_notifier.evaluate import evaluate_condition

pytestmark = pytest.mark.asyncio


async def test_evaluate_condition_numeric_true(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.states.async_set("sensor.phone_battery", 42)
    await hass.async_block_till_done()

    cond = {"entity_id": "sensor.phone_battery", "operator": ">", "value": "20"}

    assert await evaluate_condition(hass, cond)


async def test_evaluate_condition_numeric_false(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.states.async_set("sensor.phone_battery", 10)
    await hass.async_block_till_done()

    cond = {"entity_id": "sensor.phone_battery", "operator": ">", "value": "20"}

    assert not await evaluate_condition(hass, cond)


async def test_evaluate_condition_battery_level_above(
    hass: HomeAssistant, enable_custom_integrations: None
):
    """Test numeric condition for battery level above threshold."""
    hass.states.async_set("sensor.device_battery", 75)
    await hass.async_block_till_done()

    cond = {"entity_id": "sensor.device_battery", "operator": ">", "value": "50"}

    assert await evaluate_condition(hass, cond)


async def test_evaluate_condition_battery_level_below(
    hass: HomeAssistant, enable_custom_integrations: None
):
    """Test numeric condition for battery level below threshold."""
    hass.states.async_set("sensor.device_battery", 30)
    await hass.async_block_till_done()

    cond = {"entity_id": "sensor.device_battery", "operator": "<", "value": "50"}

    assert await evaluate_condition(hass, cond)


async def test_evaluate_condition_battery_level_equal(
    hass: HomeAssistant, enable_custom_integrations: None
):
    """Test numeric condition for exact battery level."""
    hass.states.async_set("sensor.device_battery", 100)
    await hass.async_block_till_done()

    cond = {"entity_id": "sensor.device_battery", "operator": "==", "value": "100"}

    assert await evaluate_condition(hass, cond)


async def test_evaluate_condition_string_true(
    hass: HomeAssistant