from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

try:  # ≥2025.7
    from homeassistant.helpers.text import slugify
except ImportError:  # ≤2025.6
    from homeassistant.util import slugify

from .const import (
    CONF_FALLBACK,
    CONF_MATCH_MODE,
    CONF_PRIORITY,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_SERVICE,
)

_LOGGER = logging.getLogger(DOMAIN)

# ────────────────────────── constants ─────────────────────────────────────────
STEP_USER = "user"
STEP_ADD_TARGET = "add_target"
STEP_ADD_COND_ENTITY = "add_condition_entity"
STEP_ADD_COND_VALUE = "add_condition_value"
STEP_COND_MORE = "condition_more"
STEP_MATCH_MODE = "match_mode"
STEP_TARGET_MORE = "target_more"
STEP_ORDER_TARGETS = "order_targets"
STEP_CHOOSE_FALLBACK = "choose_fallback"

_OPS_NUM = [">", "<", ">=", "<="]
_OPS_STR = ["==", "!="]

ENTITY_DOMAINS = [
    "sensor",
    "binary_sensor",
    "device_tracker",
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
]


# ────────────────────────── config-flow class ────────────────────────────────
class CustomDeviceNotifierConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Custom Device Notifier."""

    VERSION = 3

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._targets: list[dict[str, Any]] = []
        self._working_target: dict[str, Any] | None = None
        self._working_condition: dict[str, Any] | None = None

    # ──────────────────────────── STEP: user ─────────────────────────────────
    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors = {}

        if user_input is not None:
            raw = user_input["service_name_raw"].strip()
            if raw:
                self._data[CONF_SERVICE_NAME_RAW] = raw
                self._data["service_name"] = slugify(raw, separator="_")
                return await self.async_step_add_target()
            errors["service_name_raw"] = "required"

        schema = vol.Schema({vol.Required("service_name_raw"): str})
        return self.async_show_form(step_id=STEP_USER, data_schema=schema, errors=errors)

    # ──────────────────────── STEP: add_target ───────────────────────────────
    async def async_step_add_target(self, user_input: dict[str, Any] | None = None):
        _LOGGER.debug("STEP add_target | input=%s", user_input)
        errors: dict[str, str] = {}
        notify_services = self.hass.services.async_services().get("notify", {})

        if user_input is not None:
            svc = user_input["target_service"]
            if svc not in notify_services:
                errors["target_service"] = "must_be_notify"
            else:
                self._working_target = {KEY_SERVICE: f"notify.{svc}", KEY_CONDITIONS: []}
                return await self.async_step_condition_more()

        # Accept *any* string here; we do the real validation above
        schema = vol.Schema({vol.Required("target_service"): str})
        return self.async_show_form(step_id=STEP_ADD_TARGET, data_schema=schema, errors=errors)

    # ────────────────── STEP: add_condition_entity ───────────────────────────
    async def async_step_add_condition_entity(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}

        if user_input is not None:
            self._working_condition = {"entity_id": user_input["entity"]}
            return await self.async_step_add_condition_value()

        schema = vol.Schema(
            {
                vol.Required("entity"): selector(
                    {"entity": {"domain": ENTITY_DOMAINS, "multiple": False}}
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_ADD_COND_ENTITY, data_schema=schema, errors=errors
        )

    # ─────────────────── STEP: add_condition_value ───────────────────────────
    async def async_step_add_condition_value(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}

        if user_input is not None:
            op = user_input["operator"]
            val: int | float | str = user_input["value"]
            self._working_condition["operator"] = op
            self._working_condition["value"] = val
            self._working_target[KEY_CONDITIONS].append(self._working_condition)
            self._working_condition = None
            return await self.async_step_condition_more()

        schema = vol.Schema(
            {
                vol.Required("operator", default=">"): vol.In(_OPS_NUM + _OPS_STR),
                vol.Required("value"): vol.Any(int, float, str),
            }
        )
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE, data_schema=schema, errors=errors
        )

    # ──────────────────────── STEP: condition_more ───────────────────────────
    async def async_step_condition_more(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            if user_input["choice"] == "add":
                return await self.async_step_add_condition_entity()

            self._targets.append(self._working_target)
            self._working_target = None
            return await self.async_step_match_mode()

        schema = vol.Schema(
            {
                vol.Required("choice", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "➕ Add another condition"},
                                {"value": "done", "label": "✅ Done conditions"},
                            ]
                        }
                    }
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_COND_MORE, data_schema=schema, errors={}
        )

    # ───────────────────────── STEP: match_mode ──────────────────────────────
    async def async_step_match_mode(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._working_target[CONF_MATCH_MODE] = user_input["match_mode"]
            return await self.async_step_target_more()

        schema = vol.Schema(
            {
                vol.Required("match_mode", default="all"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "all", "label": "✅ all (AND)"},
                                {"value": "any", "label": "🔀 any (OR)"},
                            ]
                        }
                    }
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_MATCH_MODE, data_schema=schema, errors={}
        )

    # ──────────────────────── STEP: target_more ──────────────────────────────
    async def async_step_target_more(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            if user_input["next"] == "add":
                return await self.async_step_add_target()
            return await self.async_step_order_targets()

        schema = vol.Schema(
            {
                vol.Required("next", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "➕ Add another notify target"},
                                {"value": "done", "label": "✅ Done targets"},
                            ]
                        }
                    }
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_TARGET_MORE, data_schema=schema, errors={}
        )

    # ───────────────────── STEP: order_targets ───────────────────────────────
    async def async_step_order_targets(self, user_input: dict[str, Any] | None = None):
        opts = [t[KEY_SERVICE] for t in self._targets]

        if user_input is not None:
            self._data[CONF_TARGETS] = self._targets
            self._data[CONF_PRIORITY] = user_input["priority"]
            return await self.async_step_choose_fallback()

        schema = vol.Schema(
            {
                vol.Required("priority", default=opts): selector(
                    {"multi_select": {"options": opts}}
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_ORDER_TARGETS, data_schema=schema, errors={}
        )

    # ──────────────────── STEP: choose_fallback ──────────────────────────────
    async def async_step_choose_fallback(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}
        notify_services = self.hass.services.async_services().get("notify", {})

        if user_input is not None:
            fb = user_input["fallback"]
            if fb not in notify_services:
                errors["fallback"] = "must_be_notify"
            else:
                self._data[CONF_FALLBACK] = fb
                return self.async_create_entry(
                    title=self._data[CONF_SERVICE_NAME_RAW], data=self._data
                )

        default_fb = (
            self._targets[0][KEY_SERVICE].removeprefix("notify.")
            if self._targets
            else None
        )
        schema = vol.Schema(
            {
                vol.Required("fallback", default=default_fb): selector(
                    {"service": {"domain": "notify"}}
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK, data_schema=schema, errors=errors
        )

    # ───────────────────────── options-flow hook ─────────────────────────────
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        flow = CustomDeviceNotifierConfigFlow()
        flow.hass = config_entry.hass  # type: ignore[assignment]
        flow._data = dict(config_entry.data)
        flow._targets = list(config_entry.data.get(CONF_TARGETS, []))
        return flow
