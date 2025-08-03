"""Unit-test evaluate_condition helper."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.custom_device_notifier.evaluate import evaluate_condition

pytestmark = pytest.mark.asyncio


async def test_evaluate_condition_numeric_true(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.states.async_set("sensor.phone_battery", 42)
    await hass.async_block_till_done()

    cond = {
        "condition": "numeric_state",
        "entity_id": ["sensor.phone_battery"],
        "above": 20,
    }

    assert await evaluate_condition(hass, cond)


async def test_evaluate_condition_numeric_false(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.states.async_set("sensor.phone_battery", 10)
    await hass.async_block_till_done()

    cond = {
        "condition": "numeric_state",
        "entity_id": ["sensor.phone_battery"],
        "above": 20,
    }

    assert not await evaluate_condition(hass, cond)


async def test_evaluate_condition_string_true(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.states.async_set("binary_sensor.door", "on")
    await hass.async_block_till_done()

    cond = {
        "condition": "state",
        "entity_id": ["binary_sensor.door"],
        "state": "on",
    }

    assert await evaluate_condition(hass, cond)


async def test_evaluate_condition_string_false(
    hass: HomeAssistant, enable_custom_integrations: None
):
    hass.states.async_set("binary_sensor.door", "off")
    await hass.async_block_till_done()

    cond = {
        "condition": "state",
        "entity_id": ["binary_sensor.door"],
        "state": "on",
    }

    assert not await evaluate_condition(hass, cond)


async def test_evaluate_condition_unknown_entity(
    hass: HomeAssistant, enable_custom_integrations: None
):
    cond = {
        "condition": "state",
        "entity_id": ["binary_sensor.unknown"],
        "state": "on",
    }

    with pytest.raises(Exception):  # Catch ConditionError or similar
        await evaluate_condition(hass, cond)


# Add tests for other condition types, like template, time, etc., if supported in evaluate.py
