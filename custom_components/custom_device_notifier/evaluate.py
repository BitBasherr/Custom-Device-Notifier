from homeassistant.helpers import condition


def evaluate_condition(hass, cond: dict) -> bool:
    checker = condition.from_config(cond)
    return checker(hass, {})
