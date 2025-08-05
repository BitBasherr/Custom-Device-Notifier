# custom_components/custom_device_notifier/evaluate.py
from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import condition


async def evaluate_condition(hass: HomeAssistant, cfg: Mapping[str, Any]) -> bool:
    """Evaluate a condition (custom or native HA format)."""
    cfg = dict(cfg)

    # ── Native Home Assistant format ──────────────────────────────────────────
    if "condition" in cfg:
        checker = await condition.async_from_config(hass, cfg)
        raw_result = checker(hass, {})
        if isinstance(raw_result, Awaitable):
            raw_result = await raw_result
        return bool(raw_result)

    # ── Custom compact format -------------------------------------------------
    entity_id = cfg["entity_id"]
    operator = cfg["operator"]
    value = cfg["value"]

    # Allow single entity_id or list
    entity_ids = entity_id if isinstance(entity_id, list) else [entity_id]
    matches: list[bool] = []

    for eid in entity_ids:
        state = hass.states.get(eid)
        if state is None:
            matches.append(False)
            continue

        # ---------- Special strings ------------------------------------------
        if value in ["unknown", "unavailable", "unknown or unavailable"]:
            if operator not in ("==", "!="):
                raise ValueError("Special values only support == or !=")

            template_str = (
                f"{{{{ states('{eid}') in ['unknown', 'unavailable'] }}}}"
                if value == "unknown or unavailable"
                else f"{{{{ is_state('{eid}', '{value}') }}}}"
            )
            if operator == "!=":
                template_str = f"{{{{ not ({template_str[3:-3]}) }}}}"

            ha_cfg: dict[str, Any] = {
                "condition": "template",
                "value_template": template_str,
            }

        # ---------- Everything else -----------------------------------------
        else:
            try:
                float(state.state)  # will raise if not numeric
                is_numeric = True
            except ValueError:
                is_numeric = False

            # ----- Non-numeric comparisons ----------------------------------
            if not is_numeric:
                if operator in (">", "<", ">=", "<="):
                    matches.append(False)
                    continue
                if operator not in ("==", "!="):
                    raise ValueError("String conditions only support == or !=")

                if operator == "==":
                    ha_cfg = {
                        "condition": "state",
                        "entity_id": [eid],
                        "state": str(value),
                    }
                else:  # "!="
                    ha_cfg = {
                        "condition": "template",
                        "value_template": f"{{{{ not is_state('{eid}', '{value}') }}}}",
                    }

            # ----- Numeric comparisons --------------------------------------
            else:
                ha_cfg: dict[str, Any] = {
                    "condition": "numeric_state",
                    "entity_id": [eid],
                }
                if operator == "==":
                    ha_cfg = {
                        "condition": "state",
                        "entity_id": [eid],
                        "state": str(value),
                    }
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
                        "value_template": (
                            f"{{{{ states('{eid}') | float != {value} | float }}}}"
                        ),
                    }
                else:
                    raise ValueError("Invalid operator for numeric value")

        # ---------- Execute the compiled HA condition -----------------------
        checker = await condition.async_from_config(hass, ha_cfg)
        raw_result = checker(hass, {})
        if isinstance(raw_result, Awaitable):
            raw_result = await raw_result
        matches.append(bool(raw_result))

    return any(matches)
