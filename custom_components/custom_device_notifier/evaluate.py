# custom_components/custom_device_notifier/evaluate.py
from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
async def evaluate_condition(hass: HomeAssistant, cfg: Mapping[str, Any]) -> bool:
    entity_id = cfg["entity_id"]
    operator = cfg["operator"]
    value = cfg["value"]

    state = hass.states.get(entity_id)
    if state is None:
        return False

    # Handle special string values (e.g., "unknown or unavailable")
    if value in ["unknown", "unavailable", "unknown or unavailable"]:
        if operator != "==" and operator != "!=":
            raise ValueError("Special values only support == or !=")
        template_str = (
            f"{{{{ states('{entity_id}') in ['unknown', 'unavailable'] }}}}"
            if value == "unknown or unavailable"
            else f"{{{{ is_state('{entity_id}', '{value}') }}}}"
        )
        if operator == "!=":
            template_str = f"{{{{ not ({template_str[3:-3]}) }}}}"  # Invert
        ha_cfg = {"condition": "template", "value_template": template_str}

    else:
        try:
            _ = float(state.state)  # Check if numeric
            is_numeric = True
        except ValueError:
            is_numeric = False

        if not is_numeric:
            if operator not in ["==", "!="]:
                raise ValueError("String conditions only support == or !=")
            ha_cfg = {"condition": "state", "entity_id": entity_id, "state": value}
            if operator == "!=":
                ha_cfg = {
                    "condition": "template",
                    "value_template": f"{{{{ not is_state('{entity_id}', '{value}') }}}}",
                }
        else:
            if operator == "==":
                ha_cfg = {
                    "condition": "numeric_state",
                    "entity_id": entity_id,
                    "value": str(value),
                }
            elif operator == ">":
                ha_cfg = {
                    "condition": "numeric_state",
                    "entity_id": entity_id,
                    "above": str(value),
                }
            elif operator == "<":
                ha_cfg = {
                    "condition": "numeric_state",
                    "entity_id": entity_id,
                    "below": str(value),
                }
            elif operator == ">=":
                ha_cfg = {
                    "condition": "numeric_state",
                    "entity_id": entity_id,
                    "above": str(float(value) - 0.0001),
                }  # Approximate >=
            elif operator == "<=":
                ha_cfg = {
                    "condition": "numeric_state",
                    "entity_id": entity_id,
                    "below": str(float(value) + 0.0001),
                }  # Approximate <=
            elif operator == "!=":
                ha_cfg = {
                    "condition": "template",
                    "value_template": f"{{{{ states('{entity_id}') | float != {value} | float }}}}",
                }
            else:
                raise ValueError("Invalid operator for numeric")

    checker = await condition.async_from_config(hass, ha_cfg)
    raw_result = checker(hass, {})
    if isinstance(raw_result, Awaitable):
        raw_result = await raw_result
    return bool(raw_result)
