from homeassistant.helpers import condition


def evaluate_condition(hass, cond: dict) -> bool:
    return condition.async_from_config(cond, False)(hass)
