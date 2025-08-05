# custom_components/custom_device_notifier/evaluate.py
from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition
from homeassistant.helpers.template import Template


async def evaluate_condition(hass: HomeAssistant, cfg: Mapping[str, Any]) -> bool:
    """Evaluate a condition (custom or native HA format)."""
    cfg = dict(cfg)

    if "condition" in cfg:
        # Native HA format - pass directly
        if "value_template" in cfg:
            cfg["value_template"] = Template(cfg["value_template"], hass)
        checker = await condition.async_from_config(hass, cfg)
        raw_result = checker(hass, {})
        if isinstance(raw_result, Awaitable):
            raw_result = await raw_result
        return bool(raw_result)

    # Custom format
    entity_id = cfg["entity_id"]
    operator = cfg["operator"]
    value = cfg["value"]

    # Handle multiple entity_ids (HA handles 'any' match for lists)
    entity_ids = entity_id if isinstance(entity_id, list) else [entity_id]

    # Build ha_cfg with entity_ids as list
    ha_cfg = {"entity_id": entity_ids}

    # Handle special string values (template for unavailable/unknown)
    if value in ["unknown", "unavailable", "unknown or unavailable"]:
        if operator != "==" and operator != "!=":
            raise ValueError("Special values only support == or !=")
        template_str = (
            "{{ states(entity_id) in ['unknown', 'unavailable'] }}"
            if value == "unknown or unavailable"
            else "{{ is_state(entity_id, value) }}"
        )
        if operator == "!=":
            template_str = f"{{ not ({template_str[3:-3]}) }}"
        ha_cfg = {"condition": "template", "value_template": template_str.replace("entity_id", "'%s'" % entity_ids[0]) if len(entity_ids) == 1 else template_str}  # Simplify for single
    else:
        # Check if numeric (sample first state)
        state = hass.states.get(entity_ids[0]) if entity_ids else None
        is_numeric = False
        if state:
            try:
                float(state.state)
                is_numeric = True
            except ValueError:
                pass

        if not is_numeric:
            if operator in [">", "<", ">=", "<="]:
                return False  # Non-numeric can't satisfy numeric ops
            if operator not in ["==", "!="]:
                raise ValueError("String conditions only support == or !=")
            ha_cfg["condition"] = "state"
            ha_cfg["state"] = value
            if operator == "!=":
                ha_cfg = {
                    "condition": "template",
                    "value_template": "{{ not is_state(entity_id, value) }}".replace("entity_id", "'%s'" % entity_ids[0]).replace("value", "'%s'" % value) if len(entity_ids) == 1 else "{{ not is_state(entity_id, value) }}",
                }
        else:
            ha_cfg["condition"] = "numeric_state"
            if operator == "==":
                ha_cfg["value"] = str(value)
            elif operator == ">":
                ha_cfg["above"] = str(value)
            elif operator == "<":
                ha_cfg["below"] = str(value)
            elif operator == ">=":
                ha_cfg["above"] = str(float(value) - 0.0001)
            elif operator == "<=":
                ha_cfg["below"] = str(float(value) + 0.0001)
            elif operator == "!=":
                ha_cfg = {
                    "condition": "template",
                    "value_template": "{{ states(entity_id) | float != value | float }}".replace("entity_id", "'%s'" % entity_ids[0]).replace("value", value) if len(entity_ids) == 1 else "{{ states(entity_id) | float != value | float }}",
                }
            else:
                raise ValueError("Invalid operator for numeric")

    checker = await condition.async_from_config(hass, ha_cfg)
    raw_result = checker(hass, {})
    if isinstance(raw_result, Awaitable):
        raw_result = await raw_result
    return bool(raw_result)