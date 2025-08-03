# custom_components/custom_device_notifier/evaluate.py
"""Helpers to evaluate a single HA condition dict."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition
from homeassistant.helpers.condition import ConditionCheckerType

ConditionDict = Mapping[str, Any]


async def evaluate_condition(hass: HomeAssistant, cfg: ConditionDict) -> bool:
    """Return True if *cfg* matches in the current HA context."""
    # cfg (dict) ➜ hass (instance)  ✅
    checker: ConditionCheckerType = await condition.async_from_config(cfg, hass)

    # checker returns bool | None; coerce to bool for MyPy
    return bool(checker(hass, {}))  # no template variables needed