from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
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
STEP_SELECT_COND_TO_EDIT = "select_condition_to_edit"
STEP_MATCH_MODE = "match_mode"
STEP_TARGET_MORE = "target_more"
STEP_ORDER_TARGETS = "order_targets"
STEP_CHOOSE_FALLBACK = "choose_fallback"
STEP_SELECT_TARGET_TO_EDIT = "select_target_to_edit"
STEP_SELECT_TARGET_TO_REMOVE = "select_target_to_remove"

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


# ──────────────────────────── helper utils ────────────────────────────────
def _order_placeholders(
    services: list[str], current: list[str] | None
) -> dict[str, str]:
    """Build description placeholders for the order/priority step."""
    if not services:
        return {"current_order": "", "remaining": ""}

    current = current or []
    chosen = [s for s in current if s in services]
    remaining = [s for s in services if s not in chosen]

    def fmt(lst: list[str]) -> str:
        if not lst:
            return "—"
        return "\n".join(f"{i + 1}. {name}" for i, name in enumerate(lst))

    return {
        "current_order": fmt(chosen),
        "remaining": "\n".join(remaining) if remaining else "—",
    }


# ──────────────────────────── Config Flow ────────────────────────────────
class CustomDeviceNotifierConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Interactive setup for Custom Device Notifier."""

    VERSION = 3

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._targets: list[dict[str, Any]] = []
        self._working_target: dict[str, Any] = {}
        self._working_condition: dict[str, Any] = {}
        self._editing_target_index: int | None = None
        self._editing_condition_index: int | None = None
        # dynamic priority builder state
        self._ordering_targets_remaining: list[str] | None = None
        self._priority_list: list[str] | None = None

    # ───────── overview helpers ─────────

    def _get_targets_overview(self) -> str:
        lines: list[str] = []
        for tgt in self._targets:
            svc = tgt.get(KEY_SERVICE, "(unknown)")
            conds = tgt.get(KEY_CONDITIONS, [])
            if conds:
                cond_desc = "; ".join(
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                )
                lines.append(f"{svc}: {cond_desc}")
            else:
                lines.append(f"{svc}: (no conditions)")
        if self._working_target.get(KEY_SERVICE):
            svc = self._working_target[KEY_SERVICE]
            conds = self._working_target.get(KEY_CONDITIONS, [])
            if conds:
                cond_desc = "; ".join(
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                )
                lines.append(f"{svc} (editing): {cond_desc}")
            else:
                lines.append(f"{svc} (editing): (no conditions)")
        return "\n".join(lines) if lines else "No targets yet"

    def _get_target_names_overview(self) -> str:
        names: list[str] = [t.get(KEY_SERVICE, "(unknown)") for t in self._targets]
        if self._working_target.get(KEY_SERVICE):
            names.append(self._working_target[KEY_SERVICE] + " (editing)")
        return "\n".join(names) if names else "No targets yet"

    def _get_condition_more_placeholders(self) -> dict[str, str]:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        return {
            "current_conditions": "\n".join(
                f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds
            )
            or "No conditions yet"
        }

    def _get_target_more_placeholders(self) -> dict[str, str]:
        return {"current_targets": self._get_target_names_overview()}

    # ───────── dynamic order helpers ─────────

    def _order_bootstrap(self) -> None:
        """Initialize (or reinitialize) the order-building state."""
        services = [t[KEY_SERVICE] for t in self._targets]
        if self._priority_list is None:
            self._priority_list = []
        if self._ordering_targets_remaining is None:
            self._ordering_targets_remaining = [
                s for s in services if s not in self._priority_list
            ]

    def _get_order_targets_schema(self) -> vol.Schema:
        """Schema for the dynamic ordering step."""
        services = [t[KEY_SERVICE] for t in self._targets]
        self._order_bootstrap()
        return vol.Schema(
            {
                vol.Optional("priority", default=self._priority_list): selector(
                    {"select": {"options": services, "multiple": True}}
                ),
                vol.Optional("next_priority"): selector(
                    {"select": {"options": self._ordering_targets_remaining or []}}
                ),
                vol.Required("nav", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "Add to order"},
                                {"value": "reset", "label": "Reset"},
                                {"value": "done", "label": "Done"},
                            ]
                        }
                    }
                ),
            }
        )

    # ───────── schema helpers ─────────

    def _get_condition_value_schema(self, entity_id: str) -> vol.Schema:
        st = self.hass.states.get(entity_id)

        is_num = False
        if st:
            try:
                float(st.state)
                is_num = True
            except ValueError:
                pass

        prev_op: str | None = None
        prev_value: str | None = None
        use_prev = False
        if (
            self._working_condition
            and self._working_condition.get("entity_id") == entity_id
        ):
            prev_op = self._working_condition.get("operator")
            prev_value = self._working_condition.get("value")
            use_prev = prev_op is not None and prev_value is not None

        if is_num:
            num_sel = (
                {"number": {"min": 0, "max": 100, "step": 1}}
                if "battery" in entity_id
                else {"number": {}}
            )

            value_options = [
                {"value": "manual", "label": "Enter manually"},
                {
                    "value": "current",
                    "label": f"Current state: {st.state}" if st else "Current (unknown)",
                },
            ]

            default_operator = prev_op if use_prev else ">"
            default_value_choice = (
                "current"
                if (use_prev and st and str(prev_value) == str(st.state))
                else "manual"
            )

            default_num_value = 0.0
            if use_prev and prev_value is not None:
                try:
                    default_num_value = float(prev_value)
                except (TypeError, ValueError):
                    default_num_value = float(st.state) if st else 0.0
            else:
                default_num_value = float(st.state) if st else 0.0

            return vol.Schema(
                {
                    vol.Required("operator", default=default_operator): selector(
                        {"select": {"options": _OPS_NUM}}
                    ),
                    vol.Required(
                        "value_choice", default=default_value_choice
                    ): selector({"select": {"options": value_options}}),
                    vol.Optional("value", default=default_num_value): selector(num_sel),
                    vol.Optional("manual_value"): str,
                }
            )

        # string path
        opts: list[str] = ["unknown or unavailable"]
        if st:
            opts.append(st.state)
        opts.extend(["unknown", "unavailable"])
        if (
            "_last_update_trigger" in entity_id
            and "android.intent.action.ACTION_SHUTDOWN" not in opts
        ):
            opts.append("android.intent.action.ACTION_SHUTDOWN")
        uniq = list(dict.fromkeys(opts))

        default_operator = prev_op if use_prev else "=="

        value_options = [
            {"value": "manual", "label": "Enter manually"},
            {
                "value": "current",
                "label": f"Current state: {st.state}" if st else "Current (unknown)",
            },
        ]
        default_value_choice = (
            "current"
            if (use_prev and st and str(prev_value) == str(st.state))
            else "manual"
        )

        if use_prev and prev_value in uniq:
            default_str_value = prev_value
        else:
            default_str_value = uniq[0] if uniq else ""

        choices: list[dict[str, str]] = []
        for opt in uniq:
            if opt == "android.intent.action.ACTION_SHUTDOWN":
                choices.append({"value": opt, "label": "Shutdown as Last Update"})
            else:
                choices.append({"value": opt, "label": opt})

        return vol.Schema(
            {
                vol.Required("operator", default=default_operator): selector(
                    {"select": {"options": _OPS_STR}}
                ),
                vol.Required("value_choice", default=default_value_choice): selector(
                    {"select": {"options": value_options}}
                ),
                vol.Optional("value", default=default_str_value): selector(
                    {"select": {"options": choices}}
                ),
                vol.Optional("manual_value"): str,
            }
        )

    def _get_condition_more_schema(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add"},
            {"value": "done", "label": "✅ Done"},
        ]
        if self._working_target.get(KEY_CONDITIONS):
            options.insert(1, {"value": "edit", "label": "✏️ Edit"})
            options.insert(2, {"value": "remove", "label": "➖ Remove"})
        return vol.Schema(
            {
                vol.Required("choice", default="add"): selector(
                    {"select": {"options": options}}
                )
            }
        )

    def _get_target_more_schema(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add target"},
            {"value": "done", "label": "✅ Done"},
        ]
        if self._targets:
            options.insert(1, {"value": "edit", "label": "✏️ Edit target"})
            options.insert(2, {"value": "remove", "label": "➖ Remove target"})
        return vol.Schema(
            {
                vol.Required("next", default="add"): selector(
                    {"select": {"options": options}}
                )
            }
        )

    def _get_choose_fallback_schema(self) -> vol.Schema:
        """Schema for fallback pick (with nav to support tests)."""
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        default_fb = (
            self._targets[0][KEY_SERVICE].removeprefix("notify.")
            if self._targets
            else ""
        )
        return vol.Schema(
            {
                vol.Required("fallback", default=default_fb): selector(
                    {"select": {"options": service_options, "custom_value": True}}
                ),
                vol.Optional("nav", default="continue"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "back", "label": "⬅ Back"},
                                {"value": "continue", "label": "Continue"},
                            ]
                        }
                    }
                ),
            }
        )

    # ───────── STEP: user ─────────
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP user | input=%s", user_input)
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
    async def async_step_add_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_target | input=%s", user_input)
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)

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

        return self.async_show_form(
            step_id=STEP_ADD_TARGET,
            data_schema=vol.Schema(
                {
                    vol.Required("target_service"): selector(
                        {"select": {"options": service_options, "custom_value": True}}
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(service_options),
                "current_targets": self._get_targets_overview(),
            },
        )

    # ─── STEP: add_condition_entity ───
    async def async_step_add_condition_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_condition_entity | input=%s", user_input)
        if not user_input:
            notify_service = self._working_target.get(KEY_SERVICE)
            all_entities = [
                entity
                for entity in self.hass.states.async_entity_ids()
                if entity.split(".")[0] in ENTITY_DOMAINS
            ]
            options: list[str] = []
            if notify_service:
                slug = notify_service.removeprefix("notify.")
                tokens = [tok for tok in slug.split("_") if tok]
                generic = {"mobile", "app", "notify", "mobileapp"}
                tokens = [t for t in tokens if t not in generic]

                def weight(entity: str) -> tuple[int, ...]:
                    return tuple(int(tok in entity) for tok in tokens)

                options = sorted(
                    all_entities, key=lambda e: (weight(e), e), reverse=True
                )
            else:
                options = sorted(all_entities)

            return self.async_show_form(
                step_id=STEP_ADD_COND_ENTITY,
                data_schema=vol.Schema(
                    {
                        vol.Required("entity"): selector(
                            {"select": {"options": options, "custom_value": True}}
                        )
                    }
                ),
            )

        self._working_condition = {"entity_id": user_input["entity"]}
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(user_input["entity"]),
            description_placeholders={"entity_id": user_input["entity"]},
        )

    # ─── STEP: add_condition_value ───
    async def async_step_add_condition_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_condition_value | input=%s", user_input)
        if user_input:
            final_value = user_input.get("manual_value") or user_input.get("value")
            if isinstance(final_value, (int, float)):
                if float(final_value).is_integer():
                    final_value = str(int(final_value))
                else:
                    final_value = str(final_value)
            else:
                final_value = str(final_value)

            self._working_condition.update(
                operator=user_input["operator"], value=final_value
            )
            if self._editing_condition_index is not None:
                self._working_target[KEY_CONDITIONS][self._editing_condition_index] = (
                    self._working_condition
                )
                self._editing_condition_index = None
            else:
                self._working_target[KEY_CONDITIONS].append(self._working_condition)
            self._working_condition = {}
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        eid = self._working_condition["entity_id"]
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(eid),
            description_placeholders={"entity_id": eid},
        )

    # ─── STEP: condition_more ───
    async def async_step_condition_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP condition_more | input=%s", user_input)
        if user_input:
            choice = user_input["choice"]
            if choice == "add":
                return await self.async_step_add_condition_entity()
            if choice == "edit":
                return await self.async_step_select_condition_to_edit()
            if choice == "remove":
                conds = self._working_target[KEY_CONDITIONS]
                labels = [
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                ]
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
            if choice == "done":
                return self.async_show_form(
                    step_id=STEP_MATCH_MODE,
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_MATCH_MODE,
                                default=self._working_target.get(
                                    CONF_MATCH_MODE, "all"
                                ),
                            ): selector(
                                {
                                    "select": {
                                        "options": [
                                            {
                                                "value": "all",
                                                "label": "Require all conditions",
                                            },
                                            {
                                                "value": "any",
                                                "label": "Require any condition",
                                            },
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
    async def async_step_remove_condition(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP remove_condition | input=%s", user_input)
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

    # ─── STEP: select_condition_to_edit ───
    async def async_step_select_condition_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_condition_to_edit | input=%s", user_input)
        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]

        if user_input:
            selected = user_input["condition"]
            index = labels.index(selected)
            self._editing_condition_index = index
            self._working_condition = self._working_target[KEY_CONDITIONS][index].copy()
            return self.async_show_form(
                step_id=STEP_ADD_COND_VALUE,
                data_schema=self._get_condition_value_schema(
                    self._working_condition["entity_id"]
                ),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_SELECT_COND_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("condition"): selector({"select": {"options": labels}})}
            ),
        )

    # ─── STEP: match_mode ───
    async def async_step_match_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP match_mode | input=%s", user_input)
        if user_input:
            selected_mode = user_input[CONF_MATCH_MODE]
            self._working_target[CONF_MATCH_MODE] = selected_mode
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            self._working_target = {}
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
                description_placeholders=self._get_target_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_MATCH_MODE,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MATCH_MODE,
                        default=self._working_target.get(CONF_MATCH_MODE, "all"),
                    ): selector(
                        {
                            "select": {
                                "options": [
                                    {"value": "all", "label": "Require all conditions"},
                                    {"value": "any", "label": "Require any condition"},
                                ]
                            }
                        }
                    )
                }
            ),
        )

    # ─── STEP: target_more ───
    async def async_step_target_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP target_more | input=%s", user_input)
        if user_input:
            nxt = user_input["next"]
            if nxt == "add":
                return self.async_show_form(
                    step_id=STEP_ADD_TARGET,
                    data_schema=vol.Schema(
                        {
                            vol.Required("target_service"): selector(
                                {
                                    "select": {
                                        "options": sorted(
                                            self.hass.services.async_services().get(
                                                "notify", {}
                                            )
                                        ),
                                        "custom_value": True,
                                    }
                                }
                            )
                        }
                    ),
                    description_placeholders=self._get_target_more_placeholders(),
                )
            if nxt == "edit":
                return await self.async_step_select_target_to_edit()
            if nxt == "remove":
                return await self.async_step_select_target_to_remove()
            if nxt == "done":
                services = [t[KEY_SERVICE] for t in self._targets]
                self._priority_list = []
                self._ordering_targets_remaining = services.copy()
                placeholders = _order_placeholders(services, self._priority_list)
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(),
                    description_placeholders=placeholders,
                )

        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._get_target_more_schema(),
            description_placeholders=self._get_target_more_placeholders(),
        )

    # ─── STEP: select_target_to_edit ───
    async def async_step_select_target_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_target_to_edit | input=%s", user_input)
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            selected = user_input["target"]
            index = targets.index(selected)
            self._editing_target_index = index
            self._working_target = self._targets[index].copy()
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("target"): selector({"select": {"options": targets}})}
            ),
            description_placeholders=self._get_target_more_placeholders(),
        )

    # ─── STEP: select_target_to_remove ───
    async def async_step_select_target_to_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_target_to_remove | input=%s", user_input)
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            to_remove = set(user_input.get("targets", []))
            self._targets = [
                t for i, t in enumerate(self._targets) if targets[i] not in to_remove
            ]
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
                description_placeholders=self._get_target_more_placeholders(),
            )

        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_REMOVE,
            data_schema=vol.Schema(
                {
                    vol.Optional("targets", default=[]): selector(
                        {"select": {"options": targets, "multiple": True}}
                    )
                }
            ),
            description_placeholders=self._get_target_more_placeholders(),
        )

    # ─── STEP: order_targets ───
    async def async_step_order_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP order_targets | input=%s", user_input)
        services = [t[KEY_SERVICE] for t in self._targets]
        self._order_bootstrap()
        errors: dict[str, str] = {}

        if user_input:
            nav = user_input.get("nav", "add")
            pick = user_input.get("next_priority")

            if nav == "reset":
                self._priority_list = []
                self._ordering_targets_remaining = services.copy()

            elif nav == "add":
                # Compat path: a direct 'priority' list means finalize now
                if pick is None and "priority" in user_input:
                    chosen = [s for s in user_input["priority"] if s in services]
                    leftovers = [s for s in services if s not in chosen]
                    final_order = chosen + leftovers

                    self._data.update(
                        {CONF_TARGETS: self._targets, CONF_PRIORITY: final_order}
                    )

                    notify_svcs = self.hass.services.async_services().get("notify", {})
                    service_options = sorted(notify_svcs)
                    placeholders = _order_placeholders(services, chosen)

                    return self.async_show_form(
                        step_id=STEP_CHOOSE_FALLBACK,
                        data_schema=self._get_choose_fallback_schema(),
                        errors={},
                        description_placeholders={
                            "available_services": ", ".join(service_options),
                            **placeholders,
                        },
                    )

                if not pick:
                    errors["base"] = "pick_one"
                else:
                    self._order_bootstrap()
                    assert self._priority_list is not None
                    assert self._ordering_targets_remaining is not None
                    if pick in self._ordering_targets_remaining:
                        self._priority_list.append(pick)
                        self._ordering_targets_remaining.remove(pick)

            elif nav == "done":
                chosen = (self._priority_list or []).copy()
                leftovers = [s for s in services if s not in chosen]
                final_order = chosen + leftovers

                self._data.update({CONF_TARGETS: self._targets, CONF_PRIORITY: final_order})

                notify_svcs = self.hass.services.async_services().get("notify", {})
                service_options = sorted(notify_svcs)
                placeholders = _order_placeholders(services, chosen)

                return self.async_show_form(
                    step_id=STEP_CHOOSE_FALLBACK,
                    data_schema=self._get_choose_fallback_schema(),
                    errors={},
                    description_placeholders={
                        "available_services": ", ".join(service_options),
                        **placeholders,
                    },
                )

        placeholders = _order_placeholders(services, self._priority_list or [])
        return self.async_show_form(
            step_id=STEP_ORDER_TARGETS,
            data_schema=self._get_order_targets_schema(),
            errors=errors,
            description_placeholders=placeholders,
        )

    # ─── STEP: choose_fallback ───
    async def async_step_choose_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP choose_fallback | input=%s", user_input)
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)

        if user_input:
            if user_input.get("nav") == "back":
                services = [t[KEY_SERVICE] for t in self._targets]
                placeholders = _order_placeholders(
                    services, self._data.get(CONF_PRIORITY)
                )
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(),
                    description_placeholders=placeholders,
                )

            fb = user_input["fallback"]
            if fb not in notify_svcs:
                errors["fallback"] = "must_be_notify"
            else:
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                title = (
                    self._data.get(CONF_SERVICE_NAME_RAW)
                    or self._data.get("service_name_raw")
                    or ""
                )
                return self.async_create_entry(title=title, data=self._data)

        services = [t[KEY_SERVICE] for t in self._targets]
        placeholders = _order_placeholders(services, self._data.get(CONF_PRIORITY))
        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK,
            data_schema=self._get_choose_fallback_schema(),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(service_options),
                **placeholders,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return CustomDeviceNotifierOptionsFlowHandler(config_entry)


# ──────────────────────────── Options Flow ────────────────────────────────
class CustomDeviceNotifierOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow for Custom Device Notifier."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._data = dict(config_entry.options or config_entry.data).copy()
        self._targets = list(self._data.get(CONF_TARGETS, [])).copy()
        self._working_target: dict[str, Any] = {}
        self._working_condition: dict[str, Any] = {}
        self._editing_target_index: int | None = None
        self._editing_condition_index: int | None = None
        # dynamic order state
        self._ordering_targets_remaining: list[str] | None = None
        self._priority_list: list[str] | None = None

    # --- shared helpers ---

    def _get_targets_overview(self) -> str:
        lines: list[str] = []
        for tgt in self._targets:
            svc = tgt.get(KEY_SERVICE, "(unknown)")
            conds = tgt.get(KEY_CONDITIONS, [])
            if conds:
                cond_desc = "; ".join(
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                )
                lines.append(f"{svc}: {cond_desc}")
            else:
                lines.append(f"{svc}: (no conditions)")
        if self._working_target.get(KEY_SERVICE):
            svc = self._working_target[KEY_SERVICE]
            conds = self._working_target.get(KEY_CONDITIONS, [])
            if conds:
                cond_desc = "; ".join(
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                )
                lines.append(f"{svc} (editing): {cond_desc}")
            else:
                lines.append(f"{svc} (editing): (no conditions)")
        return "\n".join(lines) if lines else "No targets yet"

    def _get_target_names_overview(self) -> str:
        names: list[str] = [t.get(KEY_SERVICE, "(unknown)") for t in self._targets]
        if self._working_target.get(KEY_SERVICE):
            names.append(self._working_target[KEY_SERVICE] + " (editing)")
        return "\n".join(names) if names else "No targets yet"

    def _get_target_more_placeholders(self) -> dict[str, str]:
        return {"current_targets": self._get_targets_overview()}

    def _get_condition_more_placeholders(self) -> dict[str, str]:
        conds = self._working_target.get(KEY_CONDITIONS, [])
        return {
            "current_conditions": "\n".join(
                f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds
            )
            or "No conditions yet"
        }

    # --- schema helpers (options) ---

    def _get_condition_value_schema(self, entity_id: str) -> vol.Schema:
        st = self.hass.states.get(entity_id)
        is_num = False
        if st:
            try:
                float(st.state)
                is_num = True
            except ValueError:
                pass

        prev_op: str | None = None
        prev_value: str | None = None
        use_prev = False
        if (
            self._working_condition
            and self._working_condition.get("entity_id") == entity_id
        ):
            prev_op = self._working_condition.get("operator")
            prev_value = self._working_condition.get("value")
            use_prev = prev_op is not None and prev_value is not None

        if is_num:
            num_sel = (
                {"number": {"min": 0, "max": 100, "step": 1}}
                if "battery" in entity_id
                else {"number": {}}
            )
            value_options = [
                {"value": "manual", "label": "Enter manually"},
                {
                    "value": "current",
                    "label": f"Current state: {st.state}" if st else "Current (unknown)",
                },
            ]
            default_operator = prev_op if use_prev else ">"
            default_value_choice = (
                "current"
                if (use_prev and st and str(prev_value) == str(st.state))
                else "manual"
            )

            default_num_value = 0.0
            if use_prev and prev_value is not None:
                try:
                    default_num_value = float(prev_value)
                except (TypeError, ValueError):
                    default_num_value = float(st.state) if st else 0.0
            else:
                default_num_value = float(st.state) if st else 0.0

            return vol.Schema(
                {
                    vol.Required("operator", default=default_operator): selector(
                        {"select": {"options": _OPS_NUM}}
                    ),
                    vol.Required(
                        "value_choice", default=default_value_choice
                    ): selector({"select": {"options": value_options}}),
                    vol.Optional("value", default=default_num_value): selector(num_sel),
                    vol.Optional("manual_value"): str,
                }
            )

        opts: list[str] = ["unknown or unavailable"]
        if st:
            opts.append(st.state)
        opts.extend(["unknown", "unavailable"])
        if (
            "_last_update_trigger" in entity_id
            and "android.intent.action.ACTION_SHUTDOWN" not in opts
        ):
            opts.append("android.intent.action.ACTION_SHUTDOWN")
        uniq = list(dict.fromkeys(opts))

        default_operator = prev_op if use_prev else "=="
        value_options = [
            {"value": "manual", "label": "Enter manually"},
            {
                "value": "current",
                "label": f"Current state: {st.state}" if st else "Current (unknown)",
            },
        ]
        default_value_choice = (
            "current"
            if (use_prev and st and str(prev_value) == str(st.state))
            else "manual"
        )
        if use_prev and prev_value in uniq:
            default_str_value = prev_value
        else:
            default_str_value = uniq[0] if uniq else ""

        choices: list[dict[str, str]] = []
        for opt in uniq:
            if opt == "android.intent.action.ACTION_SHUTDOWN":
                choices.append({"value": opt, "label": "Shutdown as Last Update"})
            else:
                choices.append({"value": opt, "label": opt})

        return vol.Schema(
            {
                vol.Required("operator", default=default_operator): selector(
                    {"select": {"options": _OPS_STR}}
                ),
                vol.Required("value_choice", default=default_value_choice): selector(
                    {"select": {"options": value_options}}
                ),
                vol.Optional("value", default=default_str_value): selector(
                    {"select": {"options": choices}}
                ),
                vol.Optional("manual_value"): str,
            }
        )

    def _get_condition_more_schema(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add"},
            {"value": "done", "label": "✅ Done"},
        ]
        if self._working_target.get(KEY_CONDITIONS):
            options.insert(1, {"value": "edit", "label": "✏️ Edit"})
            options.insert(2, {"value": "remove", "label": "➖ Remove"})
        return vol.Schema(
            {
                vol.Required("choice", default="add"): selector(
                    {"select": {"options": options}}
                )
            }
        )

    def _get_target_more_schema(self) -> vol.Schema:
        options = [
            {"value": "add", "label": "➕ Add target"},
            {"value": "done", "label": "✅ Done"},
        ]
        if self._targets:
            options.insert(1, {"value": "edit", "label": "✏️ Edit target"})
            options.insert(2, {"value": "remove", "label": "➖ Remove target"})
        return vol.Schema(
            {
                vol.Required("next", default="add"): selector(
                    {"select": {"options": options}}
                )
            }
        )

    def _get_order_targets_schema(self) -> vol.Schema:
        services = [t[KEY_SERVICE] for t in self._targets]
        self._order_bootstrap()
        return vol.Schema(
            {
                vol.Optional("priority", default=self._priority_list): selector(
                    {"select": {"options": services, "multiple": True}}
                ),
                vol.Optional("next_priority"): selector(
                    {"select": {"options": self._ordering_targets_remaining or []}}
                ),
                vol.Required("nav", default="add"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "add", "label": "Add to order"},
                                {"value": "reset", "label": "Reset"},
                                {"value": "done", "label": "Done"},
                            ]
                        }
                    }
                ),
            }
        )

    def _get_choose_fallback_schema(self) -> vol.Schema:
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        default_fb = (
            self._targets[0][KEY_SERVICE].removeprefix("notify.")
            if self._targets
            else ""
        )
        return vol.Schema(
            {
                vol.Required("fallback", default=default_fb): selector(
                    {"select": {"options": service_options, "custom_value": True}}
                ),
                vol.Optional("nav", default="continue"): selector(
                    {
                        "select": {
                            "options": [
                                {"value": "back", "label": "⬅ Back"},
                                {"value": "continue", "label": "Continue"},
                            ]
                        }
                    }
                ),
            }
        )

    # --- entry point (options) ---

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP init (options) | input=%s", user_input)
        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._get_target_more_schema(),
            description_placeholders=self._get_target_more_placeholders(),
        )

    # --- mirrors of config steps (options) ---

    async def async_step_add_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_target (options) | input=%s", user_input)
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
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

        return self.async_show_form(
            step_id=STEP_ADD_TARGET,
            data_schema=vol.Schema(
                {
                    vol.Required("target_service"): selector(
                        {"select": {"options": service_options, "custom_value": True}}
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(service_options),
                **self._get_target_more_placeholders(),
            },
        )

    async def async_step_add_condition_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_condition_entity (options) | input=%s", user_input)
        if not user_input:
            notify_service = self._working_target.get(KEY_SERVICE)
            all_entities = [
                entity
                for entity in self.hass.states.async_entity_ids()
                if entity.split(".")[0] in ENTITY_DOMAINS
            ]
            options: list[str] = []
            if notify_service:
                slug = notify_service.removeprefix("notify.")
                tokens = [tok for tok in slug.split("_") if tok]
                generic = {"mobile", "app", "notify", "mobileapp"}
                tokens = [t for t in tokens if t not in generic]

                def weight(entity: str) -> tuple[int, ...]:
                    return tuple(int(tok in entity) for tok in tokens)

                options = sorted(
                    all_entities, key=lambda e: (weight(e), e), reverse=True
                )
            else:
                options = sorted(all_entities)
            return self.async_show_form(
                step_id=STEP_ADD_COND_ENTITY,
                data_schema=vol.Schema(
                    {
                        vol.Required("entity"): selector(
                            {"select": {"options": options, "custom_value": True}}
                        )
                    }
                ),
            )
        self._working_condition = {"entity_id": user_input["entity"]}
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(user_input["entity"]),
            description_placeholders={"entity_id": user_input["entity"]},
        )

    async def async_step_add_condition_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_condition_value (options) | input=%s", user_input)
        if user_input:
            final_value = user_input.get("manual_value") or user_input.get("value")
            if isinstance(final_value, (int, float)):
                if float(final_value).is_integer():
                    final_value = str(int(final_value))
                else:
                    final_value = str(final_value)
            else:
                final_value = str(final_value)
            self._working_condition.update(
                operator=user_input["operator"], value=final_value
            )
            if self._editing_condition_index is not None:
                self._working_target[KEY_CONDITIONS][self._editing_condition_index] = (
                    self._working_condition
                )
                self._editing_condition_index = None
            else:
                self._working_target[KEY_CONDITIONS].append(self._working_condition)
            self._working_condition = {}
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )

        eid = self._working_condition["entity_id"]
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(eid),
            description_placeholders={"entity_id": eid},
        )

    async def async_step_condition_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP condition_more (options) | input=%s", user_input)
        if user_input:
            choice = user_input["choice"]
            if choice == "add":
                return await self.async_step_add_condition_entity()
            if choice == "edit":
                return await self.async_step_select_condition_to_edit()
            if choice == "remove":
                conds = self._working_target[KEY_CONDITIONS]
                labels = [
                    f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds
                ]
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
            if choice == "done":
                return self.async_show_form(
                    step_id=STEP_MATCH_MODE,
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_MATCH_MODE,
                                default=self._working_target.get(
                                    CONF_MATCH_MODE, "all"
                                ),
                            ): selector(
                                {
                                    "select": {
                                        "options": [
                                            {
                                                "value": "all",
                                                "label": "Require all conditions",
                                            },
                                            {
                                                "value": "any",
                                                "label": "Require any condition",
                                            },
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

    async def async_step_remove_condition(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP remove_condition (options) | input=%s", user_input)
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

    async def async_step_select_condition_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_condition_to_edit (options) | input=%s", user_input)
        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        if user_input:
            selected = user_input["condition"]
            index = labels.index(selected)
            self._editing_condition_index = index
            self._working_condition = self._working_target[KEY_CONDITIONS][index].copy()
            return self.async_show_form(
                step_id=STEP_ADD_COND_VALUE,
                data_schema=self._get_condition_value_schema(
                    self._working_condition["entity_id"]
                ),
                description_placeholders=self._get_condition_more_placeholders(),
            )
        return self.async_show_form(
            step_id=STEP_SELECT_COND_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("condition"): selector({"select": {"options": labels}})}
            ),
        )

    async def async_step_match_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP match_mode (options) | input=%s", user_input)
        if user_input:
            selected_mode = user_input[CONF_MATCH_MODE]
            self._working_target[CONF_MATCH_MODE] = selected_mode
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            self._working_target = {}
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
                description_placeholders=self._get_target_more_placeholders(),
            )
        return self.async_show_form(
            step_id=STEP_MATCH_MODE,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MATCH_MODE,
                        default=self._working_target.get(CONF_MATCH_MODE, "all"),
                    ): selector(
                        {
                            "select": {
                                "options": [
                                    {"value": "all", "label": "Require all conditions"},
                                    {"value": "any", "label": "Require any condition"},
                                ]
                            }
                        }
                    )
                }
            ),
        )

    async def async_step_target_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP target_more (options) | input=%s", user_input)
        if user_input:
            nxt = user_input["next"]
            if nxt == "add":
                return self.async_show_form(
                    step_id=STEP_ADD_TARGET,
                    data_schema=vol.Schema(
                        {
                            vol.Required("target_service"): selector(
                                {
                                    "select": {
                                        "options": sorted(
                                            self.hass.services.async_services().get(
                                                "notify", {}
                                            )
                                        ),
                                        "custom_value": True,
                                    }
                                }
                            )
                        }
                    ),
                    description_placeholders=self._get_target_more_placeholders(),
                )
            if nxt == "edit":
                return await self.async_step_select_target_to_edit()
            if nxt == "remove":
                return await self.async_step_select_target_to_remove()
            if nxt == "done":
                services = [t[KEY_SERVICE] for t in self._targets]
                self._priority_list = []
                self._ordering_targets_remaining = services.copy()
                placeholders = _order_placeholders(services, self._priority_list)
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(),
                    description_placeholders=placeholders,
                )
        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._get_target_more_schema(),
            description_placeholders=self._get_target_more_placeholders(),
        )

    async def async_step_select_target_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_target_to_edit (options) | input=%s", user_input)
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            selected = user_input["target"]
            index = targets.index(selected)
            self._editing_target_index = index
            self._working_target = self._targets[index].copy()
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=self._get_condition_more_schema(),
                description_placeholders=self._get_condition_more_placeholders(),
            )
        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_EDIT,
            data_schema=vol.Schema(
                {vol.Required("target"): selector({"select": {"options": targets}})}
            ),
            description_placeholders=self._get_target_more_placeholders(),
        )

    async def async_step_select_target_to_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP select_target_to_remove (options) | input=%s", user_input)
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            to_remove = set(user_input.get("targets", []))
            self._targets = [
                t for i, t in enumerate(self._targets) if targets[i] not in to_remove
            ]
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=self._get_target_more_schema(),
                description_placeholders=self._get_target_more_placeholders(),
            )
        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_REMOVE,
            data_schema=vol.Schema(
                {
                    vol.Optional("targets", default=[]): selector(
                        {"select": {"options": targets, "multiple": True}}
                    )
                }
            ),
            description_placeholders=self._get_target_more_placeholders(),
        )

    async def async_step_order_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP order_targets (options) | input=%s", user_input)
        services = [t[KEY_SERVICE] for t in self._targets]
        self._order_bootstrap()
        errors: dict[str, str] = {}

        if user_input:
            nav = user_input.get("nav", "add")
            pick = user_input.get("next_priority")

            if nav == "reset":
                self._priority_list = []
                self._ordering_targets_remaining = services.copy()

            elif nav == "add":
                # Compat path: a direct 'priority' list means finalize now
                if pick is None and "priority" in user_input:
                    chosen = [s for s in user_input["priority"] if s in services]
                    leftovers = [s for s in services if s not in chosen]
                    final_order = chosen + leftovers

                    self._data.update(
                        {CONF_TARGETS: self._targets, CONF_PRIORITY: final_order}
                    )

                    notify_svcs = self.hass.services.async_services().get("notify", {})
                    service_options = sorted(notify_svcs)
                    placeholders = _order_placeholders(services, chosen)

                    return self.async_show_form(
                        step_id=STEP_CHOOSE_FALLBACK,
                        data_schema=self._get_choose_fallback_schema(),
                        errors={},
                        description_placeholders={
                            "available_services": ", ".join(service_options),
                            **placeholders,
                        },
                    )

                if not pick:
                    errors["base"] = "pick_one"
                else:
                    self._order_bootstrap()
                    assert self._priority_list is not None
                    assert self._ordering_targets_remaining is not None
                    if pick in self._ordering_targets_remaining:
                        self._priority_list.append(pick)
                        self._ordering_targets_remaining.remove(pick)

            elif nav == "done":
                chosen = (self._priority_list or []).copy()
                leftovers = [s for s in services if s not in chosen]
                final_order = chosen + leftovers

                self._data.update({CONF_TARGETS: self._targets, CONF_PRIORITY: final_order})

                notify_svcs = self.hass.services.async_services().get("notify", {})
                service_options = sorted(notify_svcs)
                placeholders = _order_placeholders(services, chosen)

                return self.async_show_form(
                    step_id=STEP_CHOOSE_FALLBACK,
                    data_schema=self._get_choose_fallback_schema(),
                    errors={},
                    description_placeholders={
                        "available_services": ", ".join(service_options),
                        **placeholders,
                    },
                )

        placeholders = _order_placeholders(services, self._priority_list or [])
        return self.async_show_form(
            step_id=STEP_ORDER_TARGETS,
            data_schema=self._get_order_targets_schema(),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_choose_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP choose_fallback (options) | input=%s", user_input)
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        if user_input:
            if user_input.get("nav") == "back":
                services = [t[KEY_SERVICE] for t in self._targets]
                placeholders = _order_placeholders(
                    services, self._data.get(CONF_PRIORITY)
                )
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(),
                    description_placeholders=placeholders,
                )
            fb = user_input["fallback"]
            if fb not in notify_svcs:
                errors["fallback"] = "must_be_notify"
            else:
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                title = (
                    self._data.get(CONF_SERVICE_NAME_RAW)
                    or self._data.get("service_name_raw")
                    or ""
                )
                return self.async_create_entry(title=title, data=self._data)
        services = [t[KEY_SERVICE] for t in self._targets]
        placeholders = _order_placeholders(services, self._data.get(CONF_PRIORITY))
        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK,
            data_schema=self._get_choose_fallback_schema(),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(service_options),
                **placeholders,
            },
        )


# ───── expose options flow handler to Home Assistant (legacy path) ─────
@callback
def async_get_options_flow(
    config_entry: config_entries.ConfigEntry,
) -> config_entries.OptionsFlow:
    """Return the options flow handler for this config entry."""
    return CustomDeviceNotifierOptionsFlowHandler(config_entry)
