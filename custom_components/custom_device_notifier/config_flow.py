from __future__ import annotations

import logging
from typing import Any, cast

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
        # When reordering targets, these helper lists track remaining options and
        # the priority list built so far. They are reset at the start of the
        # order step and used across successive calls to async_step_order_targets.
        self._ordering_targets_remaining: list[str] | None = None
        self._priority_list: list[str] | None = None

    # ───────── placeholder helpers ─────────

    def _get_targets_overview(self) -> str:
        """Return a human-readable overview of existing targets."""
        lines: list[str] = []
        # Include any committed targets
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
        # If we're currently building a target that hasn't been saved yet,
        # include it in the overview as well so the user sees it immediately.
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

    def _get_condition_value_schema(self, entity_id: str) -> vol.Schema:
        """Return the schema for the condition value step.

        When editing an existing condition (``self._editing_condition_index`` is
        set and ``self._working_condition`` contains a prior condition), this
        method will pre-fill the operator and value fields with the existing
        values. It also reorders the ``value_choice`` options so that
        ``"manual"`` comes first and ``"current"`` comes second. For
        string-based sensors, the list of selectable values is also reordered
        to move the current state out of the first slot. In all cases the
        previously selected value is used as the default when editing.
        """
        # Look up the current state of the entity to infer numeric vs string
        st = self.hass.states.get(entity_id)

        is_num = False
        if st:
            try:
                float(st.state)
                is_num = True
            except ValueError:
                pass

        # Determine if we are editing an existing condition for this entity
        prev_op: str | None = None
        prev_value: str | None = None
        use_prev = False
        if self._working_condition:
            # Only use previous values when editing the same entity
            if self._working_condition.get("entity_id") == entity_id:
                prev_op = self._working_condition.get("operator")
                prev_value = self._working_condition.get("value")
                use_prev = prev_op is not None and prev_value is not None

        if is_num:
            # Numeric sensors: build number selector (battery gets special range)
            num_sel = (
                {"number": {"min": 0, "max": 100, "step": 1}}
                if "battery" in entity_id
                else {"number": {}}
            )

            # Options for value selection: manual first, current second
            num_value_options = [
                {"value": "manual", "label": "Enter manually"},
                {
                    "value": "current",
                    "label": f"Current state: {st.state}"
                    if st
                    else "Current (unknown)",
                },
            ]

            # Default operator is the previously selected operator if editing
            default_operator = prev_op if use_prev else ">"

            # Determine whether the previous value matches the current state
            # to decide the default for value_choice
            if use_prev and st and str(prev_value) == str(st.state):
                default_value_choice = "current"
            else:
                default_value_choice = "manual"

            # Default numeric value: previously entered value or current state
            # Use a separate variable name so that mypy infers a float type
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
                    ): selector({"select": {"options": num_value_options}}),
                    # Use default_num_value to ensure a float default
                    vol.Optional("value", default=default_num_value): selector(num_sel),
                    vol.Optional("manual_value"): str,
                }
            )
        else:
            # Build list of selectable string states. The current state is placed
            # after a more generic "unknown or unavailable" option, followed by
            # the standard "unknown" and "unavailable" entries. Deduplicate while
            # preserving order.
            opts: list[str] = ["unknown or unavailable"]
            if st:
                opts.append(st.state)
            opts.extend(["unknown", "unavailable"])
            # If the entity_id looks like a *_last_update_trigger sensor, include
            # ACTION_SHUTDOWN as a selectable string. These sensors often report
            # android.intent.action.ACTION_SHUTDOWN when the device is shutting down.
            if "_last_update_trigger" in entity_id and "android.intent.action.ACTION_SHUTDOWN" not in opts:
                opts.append("android.intent.action.ACTION_SHUTDOWN")
            uniq = list(dict.fromkeys(opts))

            default_operator = prev_op if use_prev else "=="

            str_value_options = [
                {"value": "manual", "label": "Enter manually"},
                {
                    "value": "current",
                    "label": f"Current state: {st.state}"
                    if st
                    else "Current (unknown)",
                },
            ]

            if use_prev and st and str(prev_value) == str(st.state):
                default_value_choice = "current"
            else:
                default_value_choice = "manual"

            # Default string value: use the previously stored value when editing
            # the same entity if it exists in the list. Otherwise choose the
            # first option. Use a distinct name from the numeric default.
            if use_prev and prev_value in uniq:
                default_str_value = prev_value
            else:
                default_str_value = uniq[0] if uniq else ""

            # Build a list of option dictionaries so we can assign a custom label
            # for ACTION_SHUTDOWN while keeping other states labelled as-is.
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
                    vol.Required(
                        "value_choice", default=default_value_choice
                    ): selector({"select": {"options": str_value_options}}),
                    # Use default_str_value here to ensure a string default and supply
                    # our custom-labelled choices
                    vol.Optional("value", default=default_str_value): selector(
                        {"select": {"options": choices}}
                    ),
                    vol.Optional("manual_value"): str,
                }
            )

    def _get_condition_more_schema(self) -> vol.Schema:
        """Return the schema for the condition more step (setup flow)."""
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

    def _get_condition_more_placeholders(self) -> dict[str, str]:
        """Return the placeholders for the condition more step."""
        conds = self._working_target.get(KEY_CONDITIONS, [])
        return {
            "current_conditions": "\n".join(
                f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds
            )
            or "No conditions yet"
        }

    def _get_target_more_placeholders(self) -> dict[str, str]:
        """Return placeholders for target-related steps."""
        return {
            "current_targets": self._get_targets_overview(),
        }

    def _get_target_more_schema(self) -> vol.Schema:
        """Return the schema for the target more step."""
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
        """Return the schema for the order targets step."""
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
                )
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
    async def async_step_add_condition_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_condition_value | input=%s", user_input)
        if user_input:
            final_value = user_input.get("manual_value") or user_input.get("value")
            # Convert numeric values to string, removing .0 for integers
            if isinstance(final_value, (int, float)) and final_value.is_integer():
                final_value = str(int(final_value))
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

        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(
                self._working_condition["entity_id"]
            ),
        )

    # ─── STEP: condition_more ───
    async def async_step_condition_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP condition_more | input=%s", user_input)
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
            elif choice == "edit":
                return await self.async_step_select_condition_to_edit()
            elif choice == "remove":
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
            elif choice == "done":
                # Proceed to match-mode selection for this target. Do not save the
                # working target yet; that occurs after match mode is chosen.
                return self.async_show_form(
                    step_id=STEP_MATCH_MODE,
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_MATCH_MODE, default=self._working_target.get(CONF_MATCH_MODE, "all")): selector(
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

        # When no user_input or fall-through, display condition more form
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
                c
                for i, c in enumerate(conds)
                if labels[i] not in to_remove
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
            # Identify index of selected condition
            selected = user_input["condition"]
            index = labels.index(selected)
            self._editing_condition_index = index
            self._working_condition = self._working_target[KEY_CONDITIONS][index].copy()
            # Pre-fill the schema with existing operator/value
            return self.async_show_form(
                step_id=STEP_ADD_COND_VALUE,
                data_schema=self._get_condition_value_schema(
                    self._working_condition["entity_id"]
                ),
                description_placeholders={**self._get_condition_more_placeholders()},
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
            # Persist the selected match mode under the configured key
            selected_mode = user_input[CONF_MATCH_MODE]
            self._working_target[CONF_MATCH_MODE] = selected_mode
            # Save the working target into the targets list or update existing
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            # Reset working target state
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
                    vol.Required(CONF_MATCH_MODE, default=self._working_target.get(CONF_MATCH_MODE, "all")): selector(
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
                                {"select": {"options": sorted(self.hass.services.async_services().get("notify", {})), "custom_value": True}}
                            )
                        }
                    ),
                    description_placeholders={**self._get_target_more_placeholders()},
                )
            elif nxt == "edit":
                return await self.async_step_select_target_to_edit()
            elif nxt == "remove":
                return await self.async_step_select_target_to_remove()
            elif nxt == "done":
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(),
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
        if user_input:
            self._data.update({CONF_TARGETS: self._targets, CONF_PRIORITY: user_input["priority"]})
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
    async def async_step_choose_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP choose_fallback | input=%s", user_input)
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        if user_input:
            fb = user_input["fallback"]
            if fb not in notify_svcs:
                errors["fallback"] = "must_be_notify"
            else:
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                # Use the raw service name as the entry title so it shows
                # up in the UI (e.g. "Test Notifier"). Fall back to
                # other keys if necessary. We try both the constant key
                # and the literal "service_name_raw" in case the constant
                # is unavailable.
                title = (
                    self._data.get(CONF_SERVICE_NAME_RAW)
                    or self._data.get("service_name_raw")
                    or ""
                )
                return self.async_create_entry(title=title, data=self._data)

        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK,
            data_schema=self._get_choose_fallback_schema(),
            errors=errors,
            description_placeholders={"available_services": ", ".join(service_options)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler for this config entry.

        This method enables Home Assistant to find the options flow when
        editing an existing config entry. Without it, attempting to enter
        the options flow would raise UnknownHandler.
        """
        return CustomDeviceNotifierOptionsFlowHandler(config_entry)


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
        # Helper lists for interactive reordering of targets. These are reset
        # when entering the order targets step and used across successive calls.
        self._ordering_targets_remaining: list[str] | None = None
        self._priority_list: list[str] | None = None

    def _get_targets_overview(self) -> str:
        """Return a human-readable overview of existing targets."""
        lines: list[str] = []
        # Committed targets
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
        # Working target not yet saved
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

    def _get_target_more_placeholders(self) -> dict[str, str]:
        """Return placeholders for target-related steps."""
        return {
            "current_targets": self._get_targets_overview(),
        }


    def _get_condition_value_schema(self, entity_id: str) -> vol.Schema:
        """Return the schema for the condition value step.

        During an options flow (editing an existing configuration), this helper
        pre-fills operator and value fields using previously saved values. It
        also reorders value choice options to place "manual" ahead of
        "current" and moves the current state out of the first position in the
        list of selectable values for string-based sensors.
        """
        st = self.hass.states.get(entity_id)

        is_num = False
        if st:
            try:
                float(st.state)
                is_num = True
            except ValueError:
                pass

        # Determine if we are editing an existing condition for this entity
        prev_op: str | None = None
        prev_value: str | None = None
        use_prev = False
        if self._working_condition:
            if self._working_condition.get("entity_id") == entity_id:
                prev_op = self._working_condition.get("operator")
                prev_value = self._working_condition.get("value")
                use_prev = prev_op is not None and prev_value is not None

        if is_num:
            num_sel = (
                {"number": {"min": 0, "max": 100, "step": 1}}
                if "battery" in entity_id
                else {"number": {}}
            )

            num_value_options = [
                {"value": "manual", "label": "Enter manually"},
                {
                    "value": "current",
                    "label": f"Current state: {st.state}"
                    if st
                    else "Current (unknown)",
                },
            ]

            default_operator = prev_op if use_prev else ">"

            if use_prev and st and str(prev_value) == str(st.state):
                default_value_choice = "current"
            else:
                default_value_choice = "manual"

            # Default numeric value: use a dedicated variable name so type
            # inference treats it as a float. When editing an existing
            # condition, attempt to cast the previous value to float,
            # otherwise fall back to the current state or 0.0.
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
                    ): selector({"select": {"options": num_value_options}}),
                    # Use default_num_value here to avoid assigning a string later
                    vol.Optional("value", default=default_num_value): selector(num_sel),
                    vol.Optional("manual_value"): str,
                }
            )
        else:
            opts: list[str] = ["unknown or unavailable"]
            if st:
                opts.append(st.state)
            opts.extend(["unknown", "unavailable"])
            # Include ACTION_SHUTDOWN for *_last_update_trigger sensors
            if "_last_update_trigger" in entity_id and "android.intent.action.ACTION_SHUTDOWN" not in opts:
                opts.append("android.intent.action.ACTION_SHUTDOWN")
            uniq = list(dict.fromkeys(opts))

            default_operator = prev_op if use_prev else "=="

            str_value_options = [
                {"value": "manual", "label": "Enter manually"},
                {
                    "value": "current",
                    "label": f"Current state: {st.state}"
                    if st
                    else "Current (unknown)",
                },
            ]

            if use_prev and st and str(prev_value) == str(st.state):
                default_value_choice = "current"
            else:
                default_value_choice = "manual"

            # Default string value: use the previously stored value when editing
            # the same entity if it exists in the list. Otherwise choose the
            # first option. Use a distinct name from the numeric default.
            if use_prev and prev_value in uniq:
                default_str_value = prev_value
            else:
                default_str_value = uniq[0] if uniq else ""

            # Build a choices list with custom labels for ACTION_SHUTDOWN
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
                    vol.Required(
                        "value_choice", default=default_value_choice
                    ): selector({"select": {"options": str_value_options}}),
                    # Use our custom-labelled choices for selectable values
                    vol.Optional("value", default=default_str_value): selector(
                        {"select": {"options": choices}}
                    ),
                    vol.Optional("manual_value"): str,
                }
            )

    def _get_condition_more_schema(self) -> vol.Schema:
        """Return the schema for the condition more step."""
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

    def _get_condition_more_placeholders(self) -> dict[str, str]:
        """Return the placeholders for the condition more step."""
        conds = self._working_target.get(KEY_CONDITIONS, [])
        return {
            "current_conditions": "\n".join(
                f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds
            )
            or "No conditions yet"
        }

    def _get_target_more_schema(self) -> vol.Schema:
        """Return the schema for the target more step."""
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
        """Return the schema for the order targets step."""
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
                )
            }
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initialize the options flow."""
        _LOGGER.debug("STEP init | input=%s", user_input)
        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._get_target_more_schema(),
            description_placeholders=self._get_target_more_placeholders(),
        )

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
                **self._get_target_more_placeholders(),
            },
        )

    async def async_step_add_condition_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_condition_entity | input=%s", user_input)
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

    async def async_step_add_condition_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP add_condition_value | input=%s", user_input)
        if user_input:
            final_value = user_input.get("manual_value") or user_input.get("value")
            if isinstance(final_value, (int, float)) and final_value.is_integer():
                final_value = str(int(final_value))
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

        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=self._get_condition_value_schema(
                self._working_condition["entity_id"]
            ),
        )

    async def async_step_condition_more(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP condition_more | input=%s", user_input)
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
            elif choice == "edit":
                return await self.async_step_select_condition_to_edit()
            elif choice == "remove":
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
            elif choice == "done":
                # When finishing condition editing for a target, do not save
                # the target yet. Instead prompt the user to choose whether
                # all conditions must match or any condition should match.
                # The selected match mode will be stored in the target when
                # async_step_match_mode is processed. This mirrors the
                # behaviour of the initial config flow so tests expecting
                # a match_mode step continue to pass.
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
        _LOGGER.debug("STEP remove_condition | input=%s", user_input)
        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        if user_input:
            to_remove = set(user_input.get("conditions_to_remove", []))
            self._working_target[KEY_CONDITIONS] = [
                c
                for i, c in enumerate(conds)
                if labels[i] not in to_remove
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
                description_placeholders={**self._get_condition_more_placeholders()},
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
        _LOGGER.debug("STEP match_mode | input=%s", user_input)
        if user_input:
            # Persist the selected match mode under the configured key
            selected_mode = user_input[CONF_MATCH_MODE]
            self._working_target[CONF_MATCH_MODE] = selected_mode
            # Save the working target into the targets list or update existing
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            # Reset working target state
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
                    vol.Required(CONF_MATCH_MODE, default=self._working_target.get(CONF_MATCH_MODE, "all")): selector(
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
        _LOGGER.debug("STEP target_more | input=%s", user_input)
        if user_input:
            nxt = user_input["next"]
            if nxt == "add":
                return self.async_show_form(
                    step_id=STEP_ADD_TARGET,
                    data_schema=vol.Schema(
                        {
                            vol.Required("target_service"): selector(
                                {"select": {"options": sorted(self.hass.services.async_services().get("notify", {})), "custom_value": True}}
                            )
                        }
                    ),
                    description_placeholders={**self._get_target_more_placeholders()},
                )
            elif nxt == "edit":
                return await self.async_step_select_target_to_edit()
            elif nxt == "remove":
                return await self.async_step_select_target_to_remove()
            elif nxt == "done":
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=self._get_order_targets_schema(),
                )
        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=self._get_target_more_schema(),
            description_placeholders=self._get_target_more_placeholders(),
        )

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
        if user_input:
            self._data.update({CONF_TARGETS: self._targets, CONF_PRIORITY: user_input["priority"]})
            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=self._get_choose_fallback_schema(),
                errors={},
            )
        return self.async_show_form(
            step_id=STEP_ORDER_TARGETS,
            data_schema=self._get_order_targets_schema(),
        )

    async def async_step_choose_fallback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        _LOGGER.debug("STEP choose_fallback | input=%s", user_input)
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        if user_input:
            fb = user_input["fallback"]
            if fb not in notify_svcs:
                errors["fallback"] = "must_be_notify"
            else:
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                # Preserve the original service name as the title on options save.
                # Try both the constant key and the literal "service_name_raw".
                title = (
                    self._data.get(CONF_SERVICE_NAME_RAW)
                    or self._data.get("service_name_raw")
                    or ""
                )
                return self.async_create_entry(title=title, data=self._data)
        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK,
            data_schema=self._get_choose_fallback_schema(),
            errors=errors,
            description_placeholders={"available_services": ", ".join(service_options)},
        )

# ───── expose options flow handler to Home Assistant ─────
@callback
def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
    """Return the options flow handler for this config entry."""
    return CustomDeviceNotifierOptionsFlowHandler(config_entry)
