"""Config flow for Custom Device Notifier."""

from __future__ import annotations

import logging
from typing import Any, Mapping

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback

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

# ─────────────────────────── step IDs ────────────────────────────
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

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._targets: list[dict[str, Any]] = []
        self._working_target: dict[str, Any] = {}
        self._working_condition: dict[str, Any] = {}

    def _get_condition_value_schema(self, entity_id: str) -> vol.Schema:
        """Return the schema for the condition value step."""
        from homeassistant.helpers.selector import selector

        st = self.hass.states.get(entity_id)
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
                if "battery" in entity_id
                else {"number": {}}
            )
            return vol.Schema(
                {
                    vol.Required("operator", default=">"): selector(
                        {"select": {"options": _OPS_NUM}}
                    ),
                    vol.Required("value_choice", default="current"): selector(
                        {
                            "select": {
                                "options": [
                                    {
                                        "value": "current",
                                        "label": f"Current state: {st.state}" if st else "Current (unknown)",
                                    },
                                    {"value": "manual", "label": "Enter manually"},
                                ]
                            }
                        }
                    ),
                    vol.Optional("value", default=float(st.state) if st else 0): selector(num_sel),
                    vol.Optional("manual_value"): str,
                }
            )
        else:
            opts = [st.state if st else "", "unknown or unavailable", "unknown", "unavailable"]
            uniq = list(dict.fromkeys(opts))
            return vol.Schema(
                {
                    vol.Required("operator", default="=="): selector(
                        {"select": {"options": _OPS_STR}}
                    ),
                    vol.Required("value_choice", default="current"): selector(
                        {
                            "select": {
                                "options": [
                                    {
                                        "value": "current",
                                        "label": f"Current state: {st.state}" if st else "Current (unknown)",
                                    },
                                    {"value": "manual", "label": "Enter manually"},
                                ]
                            }
                        }
                    ),
                    vol.Optional("value", default=uniq[0]): selector(
                        {"select": {"options": uniq}}
                    ),
                    vol.Optional("manual_value"): str,
                }
            )

    def _get_condition_more_schema(self) -> vol.Schema:
        """Return the schema for the condition more step."""
        from homeassistant.helpers.selector import selector

        return vol.Schema(
            {
                vol.Required("choice", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "➕ Add"},
                                {"value": "remove", "label": "➖ Remove"},
                                {"value": "done", "label": "✅ Done"},
                            ]
                        }
                    }
                )
            }
        )

    def _get_condition_more_placeholders(self) -> dict[str, str]:
        """Return the placeholders for the condition more step."""
        conds = self._working_target[KEY_CONDITIONS]
        return {"current_conditions": "\n".join(f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds) or "No conditions yet"}

    def _get_target_more_schema(self) -> vol.Schema:
        """Return the schema for the target more step."""
        from homeassistant.helpers.selector import selector

        return vol.Schema(
            {
                vol.Required("next", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "➕ Add target"},
                                {"value": "done", "label": "✅ Done"},
                            ]
                        }
                    }
                )
            }
        )

    def _get_order_targets_schema(self) -> vol.Schema:
        """Return the schema for the order targets step."""
        from homeassistant.helpers.selector import selector

        opts = [t[KEY_SERVICE] for t in self._targets]
        return vol.Schema(
            {
                vol.Required("priority", default=opts): selector(
                    {"select": {"options": opts, "multiple": True}}
                )
            }
        )

    def _get_choose_fallback_schema(self) -> vol.Schema:
        """Return the schema for the choose fallback step."""
        from homeassistant.helpers.selector import selector

        notify_svcs = self.hass.services.async_services().get("notify", {})
        svc_opts = sorted(notify_svcs)
        default_fb = self._targets[0][KEY_SERVICE].removeprefix("notify.") if self._targets else ""
        return vol.Schema(
            {
                vol.Required("fallback", default=default_fb): selector(
                    {"select": {"options": svc_opts, "custom_value": True}}
                )
            }
        )

    # ───────── STEP: user ─────────
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            raw = user_input["service_name_raw"].strip()
            slug = slugify(raw) or "custom_notifier"
            await self.async_set_unique_id(slug)
            self._abort_if_unique_id_configured()
            self._data.update({CONF_SERVICE_NAME_RAW: raw, CONF_SERVICE_NAME: slug})
            return await self.async_step_add_target()

        return self.async_show_form(
            step_id=STEP_USER,
            data_schema=vol.Schema({vol.Required("service_name_raw"): str}),
        )

    # ───────── STEP: add_target ─────────
    async def async_step_add_target(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        errors: dict[str, str] = {}

        if user_input:
            svc = user_input["target_service"]
            if svc not in notify_svcs:
                errors["target_service"] = "must_be_notify"
            else:
                self._working_target = {
                    KEY_SERVICE: f"notify.{svc}",
                    KEY_CONDITIONS: [],
                }
                return self.async_show_form(
                    step_id=STEP_COND_MORE,
                    data_schema=self._get_condition_more_schema(),
                    description_placeholders=self._get_condition_more_placeholders(),
                )

        schema = vol.Schema(
            {
                vol.Required("target_service"): selector(
                    {
                        "select": {
                            "options": service_options,
                            "custom_value": True,
                        }
                    }
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_ADD_TARGET,
            data_schema=schema,
            errors=errors,
        )

    # ─── STEP: add_condition_entity ───
    async def async_step_add_condition_entity(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        if not user_input:
            return self.async_show_form(
                step_id=STEP_ADD_COND_ENTITY,
                data_schema=vol.Schema(
                    {
                        vol.Required("entity"): selector(
                            {"entity": {"domain": ENTITY_DOMAINS}}
                        )
                    }
                ),
            )
        self._working_condition = {"entity_id": user_input["entity"]}
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(user_input["entity"]),
        )

    # ─── STEP: add_condition_value ───
    async def async_step_add_condition_value(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            final_value = user_input.get("manual_value") or user_input.get("value")
            self._working_condition.update(
                operator=user_input["operator"], value=str(final_value)
            )
            self._working_target[KEY_CONDITIONS].append(self._working_condition)
            self._working_condition = {}
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(self._working_condition["entity_id"]),
        )

    # ─── STEP: condition_more ───
    async def async_step_condition_more(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        if user_input:
            choice = user_input["choice"]
            if choice == "add":
                return self.async_show_form(
                    step_id=STEP_ADD_COND_ENTITY,
                    data_schema=vol.Schema(
                        {
                            vol.Required("entity"): selector(
                                {"entity": {"domain": ENTITY_DOMAINS}}
                            )
                        }
                    ),
                )
            elif choice == "remove":
                conds = self._working_target[KEY_CONDITIONS]
                labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
                return self.async_show_form(
                    step_id=STEP_REMOVE_COND,
                    data_schema=vol.Schema(
                        {
                            vol.Optional("conditions_to_remove", default=[]): selector(
                                {"select": {"options": labels, "multiple": True}}
                            )
                        }
                    ),
                )
            elif choice == "done":
                return self.async_show_form(
                    step_id=STEP_MATCH_MODE,
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_MATCH_MODE, default="all"): selector(
                                {
                                    "select": {
                                        "options": [
                                            {"value": "all", "label": "Match all"},
                                            {"value": "any", "label": "Match any"},
                                        ]
                                    }
                                }
                            )
                        }
                    ),
                )

        return self.async_show_form(
            step_id=STEP_COND_MORE,
            data_schema=self._get_condition_more_schema(),
            description_placeholders=self._get_condition_more_placeholders(),
        )

    # ─── STEP: remove_condition ───
    async def async_step_remove_condition(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]

        if user_input:
            to_remove = set(user_input.get("conditions_to_remove", []))
            self._working_target[KEY_CONDITIONS] = [
                c for i, c in enumerate(conds) if labels[i] not in to_remove
            ]
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_REMOVE_COND,
            data_schema=vol.Schema(
                {
                    vol.Optional("conditions_to_remove", default=[]): selector(
                        {"select": {"options": labels, "multiple": True}}
                    )
                }
            ),
        )

    # ─── STEP: match_mode ───
    async def async_step_match_mode(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        if user_input:
            self._working_target[CONF_MATCH_MODE] = user_input[CONF_MATCH_MODE]
            self._targets.append(self._working_target)
            self._working_target = {}
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
            )

        return self.async_show_form(
            step_id=STEP_MATCH_MODE,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MATCH_MODE, default="all"): selector(
                        {
                            "select": {
                                "options": [
                                    {"value": "all", "label": "Match all"},
                                    {"value": "any", "label": "Match any"},
                                ]
                            }
                        }
                    )
                }
            ),
        )

    # ─── STEP: target_more ───
    async def async_step_target_more(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            if user_input["next"] == "add":
                return await self.async_step_add_target()
            else:
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(),
                )

        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._get_target_more_schema(),
        )

    # ─── STEP: order_targets ───
    async def async_step_order_targets(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            self._data.update(
                {CONF_TARGETS: self._targets, CONF_PRIORITY: user_input["priority"]}
            )
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._get_choose_fallback_schema(),
                errors={},
            )

        return self.async_show_form(
            step_id=STEP_ORDER_TARGETS,
            data_schema=self._get_order_targets_schema(),
        )

    # ─── STEP: choose_fallback ───
    async def async_step_choose_fallback(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        notify_svcs = self.hass.services.async_services().get("notify", {})
        errors: dict[str, str] = {}

        if user_input:
            fb = user_input["fallback"]
            if fb not in notify_svcs:
                errors["fallback"] = "must_be_notify"
            else:
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                return self.async_create_entry(
                    title=self._data[CONF_SERVICE_NAME_RAW], data=self._data
                )

        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK,
            data_schema=self._get_choose_fallback_schema(),
            errors=errors,
        )

    # ─── options-flow reuse ───
    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return CustomDeviceNotifierOptionsFlowHandler(config_entry)

# ─────────────── OPTIONS FLOW HANDLER ───────────────
class CustomDeviceNotifierOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._data: dict[str, Any] = dict(config_entry.data).copy()
        self._options: dict[str, Any] = dict(config_entry.options or {}).copy()
        self._targets: list[dict[str, Any]] = list(config_entry.data.get(CONF_TARGETS, [])).copy()
        self._working_target: dict[str, Any] = {}
        self._working_condition: dict[str, Any] = {}

    def _get_condition_value_schema(self, entity_id: str) -> vol.Schema:
        """Return the schema for the condition value step."""
        from homeassistant.helpers.selector import selector

        st = self.hass.states.get(entity_id)
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
                if "battery" in entity_id
                else {"number": {}}
            )
            return vol.Schema(
                {
                    vol.Required("operator", default=">"): selector(
                        {"select": {"options": _OPS_NUM}}
                    ),
                    vol.Required("value_choice", default="current"): selector(
                        {
                            "select": {
                                "options": [
                                    {
                                        "value": "current",
                                        "label": f"Current state: {st.state}" if st else "Current (unknown)",
                                    },
                                    {"value": "manual", "label": "Enter manually"},
                                ]
                            }
                        }
                    ),
                    vol.Optional("value", default=float(st.state) if st else 0): selector(num_sel),
                    vol.Optional("manual_value"): str,
                }
            )
        else:
            opts = [st.state if st else "", "unknown or unavailable", "unknown", "unavailable"]
            uniq = list(dict.fromkeys(opts))
            return vol.Schema(
                {
                    vol.Required("operator", default="=="): selector(
                        {"select": {"options": _OPS_STR}}
                    ),
                    vol.Required("value_choice", default="current"): selector(
                        {
                            "select": {
                                "options": [
                                    {
                                        "value": "current",
                                        "label": f"Current state: {st.state}" if st else "Current (unknown)",
                                    },
                                    {"value": "manual", "label": "Enter manually"},
                                ]
                            }
                        }
                    ),
                    vol.Optional("value", default=uniq[0]): selector(
                        {"select": {"options": uniq}}
                    ),
                    vol.Optional("manual_value"): str,
                }
            )

    def _get_condition_more_schema(self) -> vol.Schema:
        """Return the schema for the condition more step."""
        from homeassistant.helpers.selector import selector

        return vol.Schema(
            {
                vol.Required("choice", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "➕ Add"},
                                {"value": "remove", "label": "➖ Remove"},
                                {"value": "done", "label": "✅ Done"},
                            ]
                        }
                    }
                )
            }
        )

    def _get_condition_more_placeholders(self) -> dict[str, str]:
        """Return the placeholders for the condition more step."""
        conds = self._working_target[KEY_CONDITIONS]
        return {"current_conditions": "\n".join(f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds) or "No conditions yet"}

    def _get_target_more_schema(self) -> vol.Schema:
        """Return the schema for the target more step."""
        from homeassistant.helpers.selector import selector

        return vol.Schema(
            {
                vol.Required("next", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "➕ Add target"},
                                {"value": "done", "label": "✅ Done"},
                            ]
                        }
                    }
                )
            }
        )

    def _get_order_targets_schema(self) -> vol.Schema:
        """Return the schema for the order targets step."""
        from homeassistant.helpers.selector import selector

        opts = [t[KEY_SERVICE] for t in self._targets]
        return vol.Schema(
            {
                vol.Required("priority", default=opts): selector(
                    {"select": {"options": opts, "multiple": True}}
                )
            }
        )

    def _get_choose_fallback_schema(self) -> vol.Schema:
        """Return the schema for the choose fallback step."""
        from homeassistant.helpers.selector import selector

        notify_svcs = self.hass.services.async_services().get("notify", {})
        svc_opts = sorted(notify_svcs)
        default_fb = self._targets[0][KEY_SERVICE].removeprefix("notify.") if self._targets else ""
        return vol.Schema(
            {
                vol.Required("fallback", default=default_fb): selector(
                    {"select": {"options": svc_opts, "custom_value": True}}
                )
            }
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Initialize the options flow."""
        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._get_target_more_schema(),
        )

    # ───────── STEP: add_target ─────────
    async def async_step_add_target(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        errors: dict[str, str] = {}

        if user_input:
            svc = user_input["target_service"]
            if svc not in notify_svcs:
                errors["target_service"] = "must_be_notify"
            else:
                self._working_target = {
                    KEY_SERVICE: f"notify.{svc}",
                    KEY_CONDITIONS: [],
                }
                return self.async_show_form(
                    step_id=STEP_COND_MORE,
                    data_schema=self._get_condition_more_schema(),
                    description_placeholders=self._get_condition_more_placeholders(),
                )

        schema = vol.Schema(
            {
                vol.Required("target_service"): selector(
                    {
                        "select": {
                            "options": service_options,
                            "custom_value": True,
                        }
                    }
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_ADD_TARGET,
            data_schema=schema,
            errors=errors,
        )

    # ─── STEP: add_condition_entity ───
    async def async_step_add_condition_entity(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        if not user_input:
            return self.async_show_form(
                step_id=STEP_ADD_COND_ENTITY,
                data_schema=vol.Schema(
                    {
                        vol.Required("entity"): selector(
                            {"entity": {"domain": ENTITY_DOMAINS}}
                        )
                    }
                ),
            )
        self._working_condition = {"entity_id": user_input["entity"]}
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(user_input["entity"]),
        )

    # ─── STEP: add_condition_value ───
    async def async_step_add_condition_value(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            final_value = user_input.get("manual_value") or user_input.get("value")
            self._working_condition.update(
                operator=user_input["operator"], value=str(final_value)
            )
            self._working_target[KEY_CONDITIONS].append(self._working_condition)
            self._working_condition = {}
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(self._working_condition["entity_id"]),
        )

    # ─── STEP: condition_more ───
    async def async_step_condition_more(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        if user_input:
            choice = user_input["choice"]
            if choice == "add":
                return self.async_show_form(
                    step_id=STEP_ADD_COND_ENTITY,
                    data_schema=vol.Schema(
                        {
                            vol.Required("entity"): selector(
                                {"entity": {"domain": ENTITY_DOMAINS}}
                            )
                        }
                    ),
                )
            elif choice == "remove":
                conds = self._working_target[KEY_CONDITIONS]
                labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
                return self.async_show_form(
                    step_id=STEP_REMOVE_COND,
                    data_schema=vol.Schema(
                        {
                            vol.Optional("conditions_to_remove", default=[]): selector(
                                {"select": {"options": labels, "multiple": True}}
                            )
                        }
                    ),
                )
            elif choice == "done":
                return self.async_show_form(
                    step_id=STEP_MATCH_MODE,
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_MATCH_MODE, default="all"): selector(
                                {
                                    "select": {
                                        "options": [
                                            {"value": "all", "label": "Match all"},
                                            {"value": "any", "label": "Match any"},
                                        ]
                                    }
                                }
                            )
                        }
                    ),
                )

        return self.async_show_form(
            step_id=STEP_COND_MORE,
            data_schema=self._get_condition_more_schema(),
            description_placeholders=self._get_condition_more_placeholders(),
        )

    # ─── STEP: remove_condition ───
    async def async_step_remove_condition(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]

        if user_input:
            to_remove = set(user_input.get("conditions_to_remove", []))
            self._working_target[KEY_CONDITIONS] = [
                c for i, c in enumerate(conds) if labels[i] not in to_remove
            ]
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_REMOVE_COND,
            data_schema=vol.Schema(
                {
                    vol.Optional("conditions_to_remove", default=[]): selector(
                        {"select": {"options": labels, "multiple": True}}
                    )
                }
            ),
        )

    # ─── STEP: match_mode ───
    async def async_step_match_mode(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        if user_input:
            self._working_target[CONF_MATCH_MODE] = user_input[CONF_MATCH_MODE]
            self._targets.append(self._working_target)
            self._working_target = {}
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
            )

        return self.async_show_form(
            step_id=STEP_MATCH_MODE,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MATCH_MODE, default="all"): selector(
                        {
                            "select": {
                                "options": [
                                    {"value": "all", "label": "Match all"},
                                    {"value": "any", "label": "Match any"},
                                ]
                            }
                        }
                    )
                }
            ),
        )

    # ─── STEP: target_more ───
    async def async_step_target_more(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            if user_input["next"] == "add":
                return await self.async_step_add_target()
            else:
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(),
                )

        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._get_target_more_schema(),
        )

    # ─── STEP: order_targets ───
    async def async_step_order_targets(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            self._data.update(
                {CONF_TARGETS: self._targets, CONF_PRIORITY: user_input["priority"]}
            )
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._get_choose_fallback_schema(),
                errors={},
            )

        return self.async_show_form(
            step_id=STEP_ORDER_TARGETS,
            data_schema=self._get_order_targets_schema(),
        )

    # ─── STEP: choose_fallback ───
    async def async_step_choose_fallback(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from homeassistant.helpers.selector import selector

        notify_svcs = self.hass.services.async_services().get("notify", {})
        errors: dict[str, str] = {}

        if user_input:
            fb = user_input["fallback"]
            if fb not in notify_svcs:
                errors["fallback"] = "must_be_notify"
            else:
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                return self.async_create_entry(title="", data=self._data)

        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK,
            data_schema=self._get_choose_fallback_schema(),
            errors=errors,
        )