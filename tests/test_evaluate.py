"""Unit-test evaluate_condition helper."""

from datetime import time as dt_time

import pytest
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
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.states.async_set("binary_sensor.door", "on")
    await hass.async_block_till_done()

    cond = {"entity_id": "binary_sensor.door", "operator": "==", "value": "on"}

    assert await evaluate_condition(hass, cond)


async def test_evaluate_condition_string_false(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.states.async_set("binary_sensor.door", "off")
    await hass.async_block_till_done()

    cond = {"entity_id": "binary_sensor.door", "operator": "==", "value": "on"}

    assert not await evaluate_condition(hass, cond)


async def test_evaluate_condition_device_tracker(
    hass: HomeAssistant, enable_custom_integrations: None
):
    """Test state condition for device tracker."""
    hass.states.async_set("device_tracker.phone", "home")
    await hass.async_block_till_done()

    cond = {"entity_id": "device_tracker.phone", "operator": "==", "value": "home"}

    assert await evaluate_condition(hass, cond)


async def test_evaluate_condition_unknown_entity(
    hass: HomeAssistant, enable_custom_integrations: None
):
    cond = {"entity_id": "binary_sensor.unknown", "operator": "==", "value": "on"}

    assert not await evaluate_condition(hass, cond)


async def test_evaluate_condition_template(
    hass: HomeAssistant, enable_custom_integrations: None
):
    """Test template condition."""
    hass.states.async_set("input_boolean.test", "on")
    await hass.async_block_till_done()

    cond = {
        "condition": "template",
        "value_template": "{{ is_state('input_boolean.test', 'on') }}",
    }

    assert await evaluate_condition(hass, cond)


async def test_evaluate_condition_time(
    hass: HomeAssistant, enable_custom_integrations: None
):
    """Test time condition with parsed times."""
    cond = {
        "condition": "time",
        "after": dt_time(0, 0, 0),
        "before": dt_time(23, 59, 59),
    }

    assert await evaluate_condition(hass, cond)


async def test_evaluate_condition_input_select(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.states.async_set("input_select.mode", "auto")
    await hass.async_block_till_done()

    cond = {"entity_id": "input_select.mode", "operator": "==", "value": "auto"}

    assert await evaluate_condition(hass, cond)


async def test_evaluate_condition_multiple_entities(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.states.async_set("sensor.temp1", 25)
    hass.states.async_set("sensor.temp2", 30)
    await hass.async_block_till_done()

    cond = {
        "entity_id": ["sensor.temp1", "sensor.temp2"],
        "operator": ">",
        "value": "20",
    }

    assert await evaluate_condition(hass, cond)  # 'any' match


async def test_evaluate_condition_unavailable_state(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.states.async_set("sensor.phone_battery", "unavailable")
    await hass.async_block_till_done()

    cond = {"entity_id": "sensor.phone_battery", "operator": ">", "value": "20"}

    assert not await evaluate_condition(hass, cond)