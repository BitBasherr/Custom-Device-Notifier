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
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    DOMAIN,
    KEY_CONDITIONS,
    KEY_SERVICE,
)

_LOGGER = logging.getLogger(DOMAIN)

# ──────────────────────────── step IDs ────────────────────────────────
STEP_USER = "user"
STEP_ADD_TARGET = "add_target"
STEP_ADD_COND_ENTITY = "add_condition_entity"
STEP_ADD_COND_VALUE = "add_condition_value"
STEP_COND_MORE = "condition_more"
STEP_REMOVE_COND = "remove_condition"
STEP_MATCH_MODE = "match_mode"
STEP_TARGET_MORE = "target_more"
STEP_ORDER_TARGETS = "order_targets"
STEP_CHOOSE_FALLBACK = "choose_fallback"

_OPS_NUM = [">", "<", ">=", "<=", "==", "!="]
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


class CustomDeviceNotifierConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Interactive setup for Custom Device Notifier."""

    VERSION = 3

    # ──────────────────────────────────────────────────────────────────────
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._targets: list[dict[str, Any]] = []
        self._working_target: dict[str, Any] | None = None
        self._working_condition: dict[str, Any] | None = None

    # ─────────────────────────── STEP: user ─────────────────────────────
    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input:
            raw = user_input["service_name_raw"].strip()
            slug = slugify(raw) or "custom_notifier"
            await self.async_set_unique_id(slug)
            self._abort_if_unique_id_configured()

            self._data[CONF_SERVICE_NAME_RAW] = raw
            self._data[CONF_SERVICE_NAME] = slug
            return await self.async_step_add_target()

        schema = vol.Schema({vol.Required("service_name_raw"): str})
        return self.async_show_form(step_id=STEP_USER, data_schema=schema)

    # ───────────────────────── STEP: add_target ─────────────────────────
    async def async_step_add_target(self, user_input: dict[str, Any] | None = None):
        notify_services = self.hass.services.async_services().get("notify", {})
        errors: dict[str, str] = {}

        if user_input:
            svc = user_input["target_service"]
            if svc not in notify_services:
                errors["target_service"] = "must_be_notify"
            else:
                self._working_target = {KEY_SERVICE: f"notify.{svc}", KEY_CONDITIONS: []}
                return await self.async_step_condition_more()

        # NOTE: use plain `str` so even an invalid entry passes schema
        # validation and we can surface a friendly “must_be_notify” error.
        schema = vol.Schema({vol.Required("target_service"): str})
        return self.async_show_form(
            step_id=STEP_ADD_TARGET, data_schema=schema, errors=errors
        )
        return self.async_show_form(
            step_id=STEP_ADD_TARGET, data_schema=schema, errors=errors
        )

    # ───────── STEP: add_condition_entity (entity picker) ──────────────
    async def async_step_add_condition_entity(self, user_input=None):
        if not user_input:
            schema = vol.Schema(
                {vol.Required("entity"): selector({"entity": {"domain": ENTITY_DOMAINS}})}
            )
            return self.async_show_form(step_id=STEP_ADD_COND_ENTITY, data_schema=schema)

        self._working_condition = {"entity_id": user_input["entity"]}
        return await self.async_step_add_condition_value()

    # ───────── STEP: add_condition_value (operator/value) ───────────────
    async def async_step_add_condition_value(self, user_input=None):
        ent_id = self._working_condition["entity_id"]
        st = self.hass.states.get(ent_id)

        if user_input:
            self._working_condition["operator"] = user_input["operator"]
            self._working_condition["value"] = str(user_input["value"])
            self._working_target[KEY_CONDITIONS].append(self._working_condition)
            self._working_condition = None
            return await self.async_step_condition_more()

        is_num = False
        if st:
            try:
                float(st.state)
                is_num = True
            except ValueError:
                pass

        if is_num:
            num_sel = (
                {"number": {"min": 0, "max": 100, "step": 1}}
                if "battery" in ent_id
                else {"number": {}}
            )
            schema = vol.Schema(
                {
                    vol.Required("operator", default=">"): selector(
                        {"select": {"options": _OPS_NUM}}
                    ),
                    vol.Required("value", default=float(st.state) if st else 0): vol.All(
                        selector(num_sel), vol.Coerce(str)
                    ),
                }
            )
        else:
            opts = [st.state if st else "", "unknown or unavailable", "unknown", "unavailable"]
            final: list[str] = []
            seen: set[str] = set()
            for o in opts:
                if o not in seen:
                    final.append(o)
                    seen.add(o)
            schema = vol.Schema(
                {
                    vol.Required("operator", default="=="): selector(
                        {"select": {"options": _OPS_STR}}
                    ),
                    vol.Required("value", default=final[0]): selector(
                        {"select": {"options": final}}
                    ),
                }
            )

        return self.async_show_form(step_id=STEP_ADD_COND_VALUE, data_schema=schema)

    # ───────────────────── STEP: condition_more ─────────────────────────
    async def async_step_condition_more(self, user_input=None):
        if user_input:
            match user_input["choice"]:
                case "add":
                    return await self.async_step_add_condition_entity()
                case "remove":
                    return await self.async_step_remove_condition()
                case "done":
                    return await self.async_step_match_mode()

        conds = self._working_target[KEY_CONDITIONS]
        cond_list = "\n".join(f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds) or "No conditions yet"

        schema = vol.Schema(
            {
                vol.Required("choice", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "➕ Add another condition"},
                                {"value": "remove", "label": "➖ Remove condition"},
                                {"value": "done", "label": "✅ Done this target"},
                            ]
                        }
                    }
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_COND_MORE,
            data_schema=schema,
            description_placeholders={"current_conditions": cond_list},
        )

    # ───────────── STEP: remove_condition ───────────────────────────────
    async def async_step_remove_condition(self, user_input=None):
        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]

        if user_input:
            to_remove = set(user_input.get("conditions_to_remove", []))
            self._working_target[KEY_CONDITIONS] = [
                c for idx, c in enumerate(conds) if labels[idx] not in to_remove
            ]
            return await self.async_step_condition_more()

        schema = vol.Schema(
            {
                vol.Optional("conditions_to_remove", default=[]): selector(
                    {"select": {"options": labels, "multiple": True}}
                )
            }
        )
        return self.async_show_form(step_id=STEP_REMOVE_COND, data_schema=schema)

    # ───────────────────── STEP: match_mode ─────────────────────────────
    async def async_step_match_mode(self, user_input=None):
        if user_input:
            self._working_target[CONF_MATCH_MODE] = user_input[CONF_MATCH_MODE]
            self._targets.append(self._working_target)
            self._working_target = None
            return await self.async_step_target_more()

        schema = vol.Schema(
            {
                vol.Required(CONF_MATCH_MODE, default="all"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "all", "label": "Match all conditions"},
                                {"value": "any", "label": "Match any condition"},
                            ]
                        }
                    }
                )
            }
        )
        return self.async_show_form(step_id=STEP_MATCH_MODE, data_schema=schema)

    # ───────────────────── STEP: target_more ────────────────────────────
    async def async_step_target_more(self, user_input=None):
        if user_input:
            return (
                await self.async_step_add_target()
                if user_input["next"] == "add"
                else await self.async_step_order_targets()
            )

        schema = vol.Schema(
            {
                vol.Required("next", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "➕ Add another target"},
                                {"value": "done", "label": "✅ Done targets"},
                            ]
                        }
                    }
                )
            }
        )
        return self.async_show_form(step_id=STEP_TARGET_MORE, data_schema=schema)

    # ───────────────────── STEP: order_targets ──────────────────────────
    async def async_step_order_targets(self, user_input=None):
        opts = [t[KEY_SERVICE] for t in self._targets]

        if user_input:
            self._data[CONF_TARGETS] = self._targets
            self._data[CONF_PRIORITY] = user_input["priority"]
            return await self.async_step_choose_fallback()

        schema = vol.Schema(
            {
                vol.Required("priority", default=opts): selector(
                    {"select": {"options": opts, "multiple": True}}
                )
            }
        )
        return self.async_show_form(step_id=STEP_ORDER_TARGETS, data_schema=schema)

    # ───────────────────── STEP: choose_fallback ─────────────────────────
    async def async_step_choose_fallback(self, user_input=None):
        notify_services = self.hass.services.async_services().get("notify", {})
        svc_opts = sorted(notify_services)

        if user_input:
            fb = user_input["fallback"]
            if fb not in notify_services:
                return self.async_show_form(
                    step_id=STEP_CHOOSE_FALLBACK,
                    data_schema=vol.Schema(
                        {
                            vol.Required("fallback", default=fb): selector(
                                {"select": {"options": svc_opts}}
                            )
                        }
                    ),
                    errors={"fallback": "must_be_notify"},
                )
            # store the full service id just like we did for targets
            self._data[CONF_FALLBACK] = f"notify.{fb}"
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
                    {"select": {"options": svc_opts}}
                )
            }
        )
        return self.async_show_form(step_id=STEP_CHOOSE_FALLBACK, data_schema=schema)

    # ─────────────── options-flow reuse ───────────────
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        flow = CustomDeviceNotifierConfigFlow()
        flow.hass = config_entry.hass  # type: ignore[assignment]
        flow._data = dict(config_entry.data)
        flow._targets = list(config_entry.data.get(CONF_TARGETS, []))
        return flow
