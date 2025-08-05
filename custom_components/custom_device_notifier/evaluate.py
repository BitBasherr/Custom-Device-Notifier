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

    # Handle multiple entity_ids (any match)
    entity_ids = entity_id if isinstance(entity_id, list) else [entity_id]
    matches = []

    for eid in entity_ids:
        state = hass.states.get(eid)
        if state is None:
            matches.append(False)
            continue

        # Handle special string values
        if value in ["unknown", "unavailable", "unknown or unavailable"]:
            if operator != "==" and operator != "!=":
                raise ValueError("Special values only support == or !=")
            template_str = (
                f"{{{{ states('{eid}') in ['unknown', 'unavailable'] }}}}"
                if value == "unknown or unavailable"
                else f"{{{{ is_state('{eid}', '{value}') }}}}"
            )
            if operator == "!=":
                template_str = f"{{{{ not ({template_str[3:-3]}) }}}}"
            ha_cfg = {"condition": "template", "value_template": template_str}

        else:
            try:
                _ = float(state.state)  # Check if numeric
                is_numeric = True
            except ValueError:
                is_numeric = False

            if not is_numeric:
                if operator in [">", "<", ">=", "<="]:
                    matches.append(False)  # Non-numeric can't satisfy numeric op
                    continue
                if operator not in ["==", "!="]:
                    raise ValueError("String conditions only support == or !=")
                ha_cfg = {"condition": "state", "entity_id": eid, "state": value}
                if operator == "!=":
                    ha_cfg = {
                        "condition": "template",
                        "value_template": f"{{{{ not is_state('{eid}', '{value}') }}}}",
                    }
            else:
                ha_cfg = {"condition": "numeric_state", "entity_id": eid}
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
                        "value_template": f"{{{{ states('{eid}') | float != {value} | float }}}}",
                    }
                else:
                    raise ValueError("Invalid operator for numeric")

        checker = await condition.async_from_config(hass, ha_cfg)
        raw_result = checker(hass, {})
        if isinstance(raw_result, Awaitable):
            raw_result = await raw_result
        matches.append(bool(raw_result))

    return any(matches)  # OR logic for multiple