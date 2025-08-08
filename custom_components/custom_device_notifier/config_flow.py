# custom_components/custom_device_notifier/config_flow.py
from __future__ import annotations

import copy
import logging
from typing import Any, Protocol, runtime_checkable, cast

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

# FlowResult typing compatibility (HA moved/typed this in recent releases)
try:
    # ≥ 2025.7
    from homeassistant.data_entry_flow import FlowResult as ConfigFlowResult  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - fallback for older cores
    from homeassistant.config_entries import ConfigFlowResult  # type: ignore[no-redef]

try:  # ≥2025.7
    from homeassistant.helpers.text import slugify
except Exception:  # ≤2025.6
    from homeassistant.util import slugify  # type: ignore[assignment]

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

_LOGGER = logging.getLogger(__name__)

# Step IDs
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

ENTITY_DOMAINS = {
    "sensor",
    "binary_sensor",
    "device_tracker",
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
}

# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────


def _format_conditions(conds: list[dict[str, Any]]) -> str:
    if not conds:
        return "No conditions yet"
    return "\n".join(f"- {c['entity_id']} {c['operator']} {c['value']}" for c in conds)


def _targets_overview(committed: list[dict[str, Any]], working: dict[str, Any]) -> str:
    lines: list[str] = []
    for tgt in committed:
        svc = tgt.get(KEY_SERVICE, "(unknown)")
        conds = tgt.get(KEY_CONDITIONS, [])
        if conds:
            lines.append(
                f"{svc}: " + "; ".join(f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds)
            )
        else:
            lines.append(f"{svc}: (no conditions)")
    if working.get(KEY_SERVICE):
        svc = working[KEY_SERVICE]
        conds = working.get(KEY_CONDITIONS, [])
        if conds:
            lines.append(
                f"{svc} (editing): "
                + "; ".join(f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds)
            )
        else:
            lines.append(f"{svc} (editing): (no conditions)")
    return "\n".join(lines) if lines else "No targets yet"


def _slug_tokens_from_notify_service(full_notify: str) -> list[str]:
    """
    Turn 'notify.mobile_app_fold_7' into tokens:
    ['mobile', 'app', 'fold', '7'] then filter out generic ones.
    """
    slug = full_notify.removeprefix("notify.")
    tokens = [tok for tok in slug.split("_") if tok]
    generic = {"mobile", "app", "notify", "mobileapp"}
    return [t for t in tokens if t not in generic]


def _sort_entities_for_target(all_entities: list[str], notify_service: str | None) -> list[str]:
    if not notify_service:
        return sorted(all_entities)

    tokens = _slug_tokens_from_notify_service(notify_service)

    if not tokens:
        return sorted(all_entities)

    def weight(entity: str) -> tuple[int, ...]:
        # high-level heuristic: later tokens are lower priority secondary matches
        return tuple(int(tok in entity) for tok in tokens)

    # Sort by match weight (descending), then alpha
    return sorted(all_entities, key=lambda e: (weight(e), e), reverse=True)


# ──────────────────────────────────────────────────────────────────────────────
# A small typing Protocol so schema-builder helpers can accept both flows
# ──────────────────────────────────────────────────────────────────────────────
@runtime_checkable
class _FlowLike(Protocol):
    hass: Any
    _targets: list[dict[str, Any]]
    _working_target: dict[str, Any]
    _working_condition: dict[str, Any]
    _editing_target_index: int | None
    _editing_condition_index: int | None
    _data: dict[str, Any]

    def async_show_form(  # type: ignore[override]
        self,
        *,
        step_id: str,
        data_schema: vol.Schema | None = None,
        errors: dict[str, str] | None = None,
        description_placeholders: dict[str, str] | None = None,
    ) -> ConfigFlowResult: ...


# ──────────────────────────────────────────────────────────────────────────────
# Schema builders (no unsupported selector keys)
# ──────────────────────────────────────────────────────────────────────────────
def _schema_condition_more(flow: _FlowLike) -> vol.Schema:
    opts = [
        {"value": "add", "label": "➕ Add"},
        {"value": "done", "label": "✅ Done"},
    ]
    if flow._working_target.get(KEY_CONDITIONS):
        opts.insert(1, {"value": "edit", "label": "✏️ Edit"})
        opts.insert(2, {"value": "remove", "label": "➖ Remove"})
    return vol.Schema(
        {vol.Required("choice", default="add"): selector({"select": {"options": opts}})}
    )


def _placeholders_conditions(flow: _FlowLike) -> dict[str, str]:
    return {"current_conditions": _format_conditions(flow._working_target.get(KEY_CONDITIONS, []))}


def _schema_target_more(flow: _FlowLike) -> vol.Schema:
    """
    "Current targets" section first (clickable to edit), then "Other options".
    No unsupported keys like 'disabled'.
    """
    options: list[dict[str, str]] = []
    if flow._targets:
        options.append({"value": "__header_current__", "label": "Current targets (click to edit):"})
        for idx, tgt in enumerate(flow._targets):
            options.append({"value": f"edit__{idx}", "label": f"Edit: {tgt.get(KEY_SERVICE, '(unknown)')}"})
        options.append({"value": "__header_other__", "label": "Other options:"})
    options.append({"value": "add", "label": "➕ Add target"})
    if flow._targets:
        options.append({"value": "edit", "label": "✏️ Edit target"})
        options.append({"value": "remove", "label": "➖ Remove target"})
    options.append({"value": "done", "label": "✅ Done"})
    return vol.Schema({vol.Required("next", default="add"): selector({"select": {"options": options}})})


def _placeholders_targets(flow: _FlowLike) -> dict[str, str]:
    return {"current_targets": _targets_overview(flow._targets, flow._working_target)}


def _schema_condition_value(flow: _FlowLike, entity_id: str) -> vol.Schema:
    st = flow.hass.states.get(entity_id)
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
    wc = flow._working_condition
    if wc and wc.get("entity_id") == entity_id:
        prev_op = cast(str | None, wc.get("operator"))
        prev_value = cast(str | None, wc.get("value"))
        use_prev = prev_op is not None and prev_value is not None

    if is_num:
        num_sel = {"number": {"min": 0, "max": 100, "step": 1}} if "battery" in entity_id else {"number": {}}
        value_opts = [
            {"value": "manual", "label": "Enter manually"},
            {"value": "current", "label": f"Current state: {st.state}" if st else "Current (unknown)"},
        ]
        default_operator = prev_op if use_prev else ">"
        if use_prev and st and str(prev_value) == str(st.state):
            default_choice = "current"
        else:
            default_choice = "manual"

        default_num = 0.0
        if use_prev and prev_value is not None:
            try:
                default_num = float(prev_value)
            except (TypeError, ValueError):
                default_num = float(st.state) if st else 0.0
        else:
            default_num = float(st.state) if st else 0.0

        return vol.Schema(
            {
                vol.Required("operator", default=default_operator): selector({"select": {"options": _OPS_NUM}}),
                vol.Required("value_choice", default=default_choice): selector({"select": {"options": value_opts}}),
                vol.Optional("value", default=default_num): selector(num_sel),
                vol.Optional("manual_value"): str,
            }
        )

    # string-y
    opts: list[str] = ["unknown or unavailable"]
    if st:
        opts.append(st.state)
    opts.extend(["unknown", "unavailable"])
    if "_last_update_trigger" in entity_id and "android.intent.action.ACTION_SHUTDOWN" not in opts:
        opts.append("android.intent.action.ACTION_SHUTDOWN")
    uniq = list(dict.fromkeys(opts))

    default_operator = prev_op if use_prev else "=="
    if use_prev and st and str(prev_value) == str(st.state):
        default_choice = "current"
    else:
        default_choice = "manual"

    default_str = prev_value if (use_prev and prev_value in uniq) else (uniq[0] if uniq else "")

    choices = [
        {"value": v, "label": "Shutdown as Last Update" if v == "android.intent.action.ACTION_SHUTDOWN" else v}
        for v in uniq
    ]

    return vol.Schema(
        {
            vol.Required("operator", default=default_operator): selector({"select": {"options": _OPS_STR}}),
            vol.Required("value_choice", default=default_choice): selector(
                {"select": {"options": [{"value": "manual", "label": "Enter manually"}, {"value": "current", "label": f"Current state: {st.state}" if st else "Current (unknown)"}]}}
            ),
            vol.Optional("value", default=default_str): selector({"select": {"options": choices}}),
            vol.Optional("manual_value"): str,
        }
    )


def _schema_order_targets(flow: _FlowLike) -> vol.Schema:
    # Keep the classic multi-select so your tests pass:
    opts = [t[KEY_SERVICE] for t in flow._targets]
    return vol.Schema({vol.Required("priority", default=opts): selector({"select": {"options": opts, "multiple": True}})})


def _schema_choose_fallback(flow: _FlowLike) -> vol.Schema:
    notify_svcs = flow.hass.services.async_services().get("notify", {})
    service_options = sorted(notify_svcs)
    default_fb = flow._targets[0][KEY_SERVICE].removeprefix("notify.") if flow._targets else ""
    return vol.Schema(
        {vol.Required("fallback", default=default_fb): selector({"select": {"options": service_options, "custom_value": True}})}
    )


# ──────────────────────────────────────────────────────────────────────────────
# Config Flow
# ──────────────────────────────────────────────────────────────────────────────
class CustomDeviceNotifierConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 3

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._targets: list[dict[str, Any]] = []
        self._working_target: dict[str, Any] = {}
        self._working_condition: dict[str, Any] = {}
        self._editing_target_index: int | None = None
        self._editing_condition_index: int | None = None

    # — user —
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

    # — add_target —
    async def async_step_add_target(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)

        if user_input:
            svc = user_input["target_service"]
            if svc not in notify_svcs:
                errors["target_service"] = "must_be_notify"
            else:
                self._working_target = {KEY_SERVICE: f"notify.{svc}", KEY_CONDITIONS: []}
                return self.async_show_form(
                    step_id=STEP_COND_MORE,
                    data_schema=_schema_condition_more(self),
                    description_placeholders=_placeholders_conditions(self),
                )

        return self.async_show_form(
            step_id=STEP_ADD_TARGET,
            data_schema=vol.Schema(
                {vol.Required("target_service"): selector({"select": {"options": service_options, "custom_value": True}})}
            ),
            errors=errors,
            description_placeholders={
                "available_services": ", ".join(service_options),
                "current_targets": _targets_overview(self._targets, self._working_target),
            },
        )

    # — add_condition_entity —
    async def async_step_add_condition_entity(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not user_input:
            all_entities = [
                ent for ent in self.hass.states.async_entity_ids() if ent.split(".")[0] in ENTITY_DOMAINS
            ]
            options = _sort_entities_for_target(all_entities, self._working_target.get(KEY_SERVICE))
            return self.async_show_form(
                step_id=STEP_ADD_COND_ENTITY,
                data_schema=vol.Schema(
                    {vol.Required("entity"): selector({"select": {"options": options, "custom_value": True}})}
                ),
            )

        self._working_condition = {"entity_id": user_input["entity"]}
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=_schema_condition_value(self, user_input["entity"]),
        )

    # — add_condition_value —
    async def async_step_add_condition_value(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            final_value: Any = user_input.get("manual_value") or user_input.get("value")
            if isinstance(final_value, (int, float)):
                # stringify; if integral like 40.0 -> "40"
                if float(final_value).is_integer():
                    final_value = str(int(final_value))
                else:
                    final_value = str(final_value)
            else:
                final_value = str(final_value)

            self._working_condition.update(operator=user_input["operator"], value=final_value)
            if self._editing_condition_index is not None:
                self._working_target[KEY_CONDITIONS][self._editing_condition_index] = self._working_condition
                self._editing_condition_index = None
            else:
                self._working_target[KEY_CONDITIONS].append(self._working_condition)
            self._working_condition = {}

            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=_schema_condition_more(self),
                description_placeholders=_placeholders_conditions(self),
            )

        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=_schema_condition_value(self, self._working_condition["entity_id"]),
        )

    # — condition_more —
    async def async_step_condition_more(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            choice = user_input["choice"]
            if choice == "add":
                return await self.async_step_add_condition_entity()
            if choice == "edit":
                return await self.async_step_select_condition_to_edit()
            if choice == "remove":
                labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in self._working_target[KEY_CONDITIONS]]
                return self.async_show_form(
                    step_id=STEP_REMOVE_COND,
                    data_schema=vol.Schema(
                        {vol.Optional("conditions_to_remove", default=[]): selector({"select": {"options": labels, "multiple": True}})}
                    ),
                )
            if choice == "done":
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

        return self.async_show_form(
            step_id=STEP_COND_MORE,
            data_schema=_schema_condition_more(self),
            description_placeholders=_placeholders_conditions(self),
        )

    # — remove_condition —
    async def async_step_remove_condition(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]

        if user_input:
            to_remove = set(user_input.get("conditions_to_remove", []))
            self._working_target[KEY_CONDITIONS] = [c for i, c in enumerate(conds) if labels[i] not in to_remove]
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=_schema_condition_more(self),
                description_placeholders=_placeholders_conditions(self),
            )

        return self.async_show_form(
            step_id=STEP_REMOVE_COND,
            data_schema=vol.Schema(
                {vol.Optional("conditions_to_remove", default=[]): selector({"select": {"options": labels, "multiple": True}})}
            ),
        )

    # — select_condition_to_edit —
    async def async_step_select_condition_to_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]

        if user_input:
            selected = user_input["condition"]
            index = labels.index(selected)
            self._editing_condition_index = index
            self._working_condition = self._working_target[KEY_CONDITIONS][index].copy()
            return self.async_show_form(
                step_id=STEP_ADD_COND_VALUE,
                data_schema=_schema_condition_value(self, self._working_condition["entity_id"]),
                description_placeholders=_placeholders_conditions(self),
            )

        return self.async_show_form(
            step_id=STEP_SELECT_COND_TO_EDIT,
            data_schema=vol.Schema({vol.Required("condition"): selector({"select": {"options": labels}})}),
        )

    # — match_mode —
    async def async_step_match_mode(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            self._working_target[CONF_MATCH_MODE] = user_input[CONF_MATCH_MODE]
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            self._working_target = {}
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=_schema_target_more(self),
                description_placeholders=_placeholders_targets(self),
            )

        return self.async_show_form(
            step_id=STEP_MATCH_MODE,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MATCH_MODE, default=self._working_target.get(CONF_MATCH_MODE, "all")): selector(
                        {"select": {"options": [{"value": "all", "label": "Require all conditions"}, {"value": "any", "label": "Require any condition"}]}}
                    )
                }
            ),
        )

    # — target_more —
    async def async_step_target_more(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            nxt = user_input["next"]
            if nxt.startswith("__header_"):
                return self.async_show_form(
                    step_id=STEP_TARGET_MORE,
                    data_schema=_schema_target_more(self),
                    description_placeholders=_placeholders_targets(self),
                )
            if nxt.startswith("edit__"):
                try:
                    idx = int(nxt.split("__", 1)[1])
                except ValueError:
                    idx = -1
                if 0 <= idx < len(self._targets):
                    self._editing_target_index = idx
                    self._working_target = copy.deepcopy(self._targets[idx])
                    return self.async_show_form(
                        step_id=STEP_COND_MORE,
                        data_schema=_schema_condition_more(self),
                        description_placeholders=_placeholders_conditions(self),
                    )
                # invalid index -> re-show
                return self.async_show_form(
                    step_id=STEP_TARGET_MORE,
                    data_schema=_schema_target_more(self),
                    description_placeholders=_placeholders_targets(self),
                )
            if nxt == "add":
                notify_svcs = self.hass.services.async_services().get("notify", {})
                return self.async_show_form(
                    step_id=STEP_ADD_TARGET,
                    data_schema=vol.Schema(
                        {vol.Required("target_service"): selector({"select": {"options": sorted(notify_svcs), "custom_value": True}})}
                    ),
                    description_placeholders=_placeholders_targets(self),
                )
            if nxt == "edit":
                return await self.async_step_select_target_to_edit()
            if nxt == "remove":
                return await self.async_step_select_target_to_remove()
            if nxt == "done":
                # Classic multi-select ordering (keeps tests happy)
                return self.async_show_form(
                    step_id=STEP_ORDER_TARGETS,
                    data_schema=_schema_order_targets(self),
                )

        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=_schema_target_more(self),
            description_placeholders=_placeholders_targets(self),
        )

    # — select_target_to_edit —
    async def async_step_select_target_to_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            selected = user_input["target"]
            index = targets.index(selected)
            self._editing_target_index = index
            self._working_target = copy.deepcopy(self._targets[index])
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=_schema_condition_more(self),
                description_placeholders=_placeholders_conditions(self),
            )

        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_EDIT,
            data_schema=vol.Schema({vol.Required("target"): selector({"select": {"options": targets}})}),
            description_placeholders=_placeholders_targets(self),
        )

    # — select_target_to_remove —
    async def async_step_select_target_to_remove(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            to_remove = set(user_input.get("targets", []))
            self._targets = [t for i, t in enumerate(self._targets) if targets[i] not in to_remove]
            return self.async_show_form(step_id=STEP_TARGET_MORE, data_schema=_schema_target_more(self))

        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_REMOVE,
            data_schema=vol.Schema(
                {vol.Optional("targets", default=[]): selector({"select": {"options": targets, "multiple": True}})}
            ),
            description_placeholders=_placeholders_targets(self),
        )

    # — order_targets —
    async def async_step_order_targets(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Expect {"priority": [list]} per tests
        if user_input:
            priority = list(user_input.get("priority", []))
            # Re-order internal targets to match 'priority'
            by_svc = {t[KEY_SERVICE]: t for t in self._targets}
            ordered: list[dict[str, Any]] = [by_svc[s] for s in priority if s in by_svc]
            # append any not present (shouldn't happen but safe)
            for t in self._targets:
                if t not in ordered:
                    ordered.append(t)
            self._targets = ordered
            self._data[CONF_TARGETS] = self._targets
            self._data[CONF_PRIORITY] = [t[KEY_SERVICE] for t in self._targets]

            return self.async_show_form(
                step_id=STEP_CHOOSE_FALLBACK,
                data_schema=_schema_choose_fallback(self),
                errors={},
            )

        return self.async_show_form(step_id=STEP_ORDER_TARGETS, data_schema=_schema_order_targets(self))

    # — choose_fallback —
    async def async_step_choose_fallback(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        if user_input:
            fb = user_input["fallback"]
            if fb not in notify_svcs:
                errors["fallback"] = "must_be_notify"
            else:
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                title = self._data.get(CONF_SERVICE_NAME_RAW) or self._data.get("service_name_raw") or ""
                return self.async_create_entry(title=title, data=self._data)

        return self.async_show_form(
            step_id=STEP_CHOOSE_FALLBACK,
            data_schema=_schema_choose_fallback(self),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> OptionsFlow:
        return CustomDeviceNotifierOptionsFlowHandler(config_entry)


# ──────────────────────────────────────────────────────────────────────────────
# Options Flow
# ──────────────────────────────────────────────────────────────────────────────
class CustomDeviceNotifierOptionsFlowHandler(OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        # Work against a copy so we only persist on save
        self._data = dict(config_entry.options or config_entry.data).copy()
        self._targets: list[dict[str, Any]] = list(self._data.get(CONF_TARGETS, []))
        self._working_target: dict[str, Any] = {}
        self._working_condition: dict[str, Any] = {}
        self._editing_target_index: int | None = None
        self._editing_condition_index: int | None = None

    def _ph_targets(self) -> dict[str, str]:
        return {"current_targets": _targets_overview(self._targets, self._working_target)}

    # init -> same “target_more” landing
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=_schema_target_more(self),  # type: ignore[arg-type]
            description_placeholders=self._ph_targets(),
        )

    async def async_step_add_target(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        service_options = sorted(notify_svcs)
        if user_input:
            svc = user_input["target_service"]
            if svc not in notify_svcs:
                errors["target_service"] = "must_be_notify"
            else:
                self._working_target = {KEY_SERVICE: f"notify.{svc}", KEY_CONDITIONS: []}
                return self.async_show_form(
                    step_id=STEP_COND_MORE,
                    data_schema=_schema_condition_more(self),  # type: ignore[arg-type]
                    description_placeholders=_placeholders_conditions(self),  # type: ignore[arg-type]
                )

        return self.async_show_form(
            step_id=STEP_ADD_TARGET,
            data_schema=vol.Schema(
                {vol.Required("target_service"): selector({"select": {"options": service_options, "custom_value": True}})}
            ),
            errors=errors,
            description_placeholders={"available_services": ", ".join(service_options), **self._ph_targets()},
        )

    async def async_step_add_condition_entity(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not user_input:
            all_entities = [
                ent for ent in self.hass.states.async_entity_ids() if ent.split(".")[0] in ENTITY_DOMAINS
            ]
            options = _sort_entities_for_target(all_entities, self._working_target.get(KEY_SERVICE))
            return self.async_show_form(
                step_id=STEP_ADD_COND_ENTITY,
                data_schema=vol.Schema(
                    {vol.Required("entity"): selector({"select": {"options": options, "custom_value": True}})}
                ),
            )
        self._working_condition = {"entity_id": user_input["entity"]}
        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=_schema_condition_value(self, user_input["entity"]),  # type: ignore[arg-type]
        )

    async def async_step_add_condition_value(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            final_value: Any = user_input.get("manual_value") or user_input.get("value")
            if isinstance(final_value, (int, float)):
                if float(final_value).is_integer():
                    final_value = str(int(final_value))
                else:
                    final_value = str(final_value)
            else:
                final_value = str(final_value)

            self._working_condition.update(operator=user_input["operator"], value=final_value)
            if self._editing_condition_index is not None:
                self._working_target[KEY_CONDITIONS][self._editing_condition_index] = self._working_condition
                self._editing_condition_index = None
            else:
                self._working_target[KEY_CONDITIONS].append(self._working_condition)
            self._working_condition = {}
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=_schema_condition_more(self),  # type: ignore[arg-type]
                description_placeholders=_placeholders_conditions(self),  # type: ignore[arg-type]
            )

        return self.async_show_form(
            step_id=STEP_ADD_COND_VALUE,
            data_schema=_schema_condition_value(self, self._working_condition["entity_id"]),  # type: ignore[arg-type]
        )

    async def async_step_condition_more(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            choice = user_input["choice"]
            if choice == "add":
                return await self.async_step_add_condition_entity()
            if choice == "edit":
                return await self.async_step_select_condition_to_edit()
            if choice == "remove":
                labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in self._working_target[KEY_CONDITIONS]]
                return self.async_show_form(
                    step_id=STEP_REMOVE_COND,
                    data_schema=vol.Schema(
                        {vol.Optional("conditions_to_remove", default=[]): selector({"select": {"options": labels, "multiple": True}})}
                    ),
                )
            if choice == "done":
                return self.async_show_form(
                    step_id=STEP_MATCH_MODE,
                    data_schema=vol.Schema(
                        {
                            vol.Required(
                                CONF_MATCH_MODE, default=self._working_target.get(CONF_MATCH_MODE, "all")
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

        return self.async_show_form(
            step_id=STEP_COND_MORE,
            data_schema=_schema_condition_more(self),  # type: ignore[arg-type]
            description_placeholders=_placeholders_conditions(self),  # type: ignore[arg-type]
        )

    async def async_step_remove_condition(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        if user_input:
            to_remove = set(user_input.get("conditions_to_remove", []))
            self._working_target[KEY_CONDITIONS] = [c for i, c in enumerate(conds) if labels[i] not in to_remove]
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=_schema_condition_more(self),  # type: ignore[arg-type]
                description_placeholders=_placeholders_conditions(self),  # type: ignore[arg-type]
            )
        return self.async_show_form(
            step_id=STEP_REMOVE_COND,
            data_schema=vol.Schema(
                {vol.Optional("conditions_to_remove", default=[]): selector({"select": {"options": labels, "multiple": True}})}
            ),
        )

    async def async_step_select_condition_to_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        conds = self._working_target[KEY_CONDITIONS]
        labels = [f"{c['entity_id']} {c['operator']} {c['value']}" for c in conds]
        if user_input:
            selected = user_input["condition"]
            index = labels.index(selected)
            self._editing_condition_index = index
            self._working_condition = copy.deepcopy(self._working_target[KEY_CONDITIONS][index])
            return self.async_show_form(
                step_id=STEP_ADD_COND_VALUE,
                data_schema=_schema_condition_value(self, self._working_condition["entity_id"]),  # type: ignore[arg-type]
                description_placeholders=_placeholders_conditions(self),  # type: ignore[arg-type]
            )

        return self.async_show_form(
            step_id=STEP_SELECT_COND_TO_EDIT,
            data_schema=vol.Schema({vol.Required("condition"): selector({"select": {"options": labels}})}),
        )

    async def async_step_match_mode(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            self._working_target[CONF_MATCH_MODE] = user_input[CONF_MATCH_MODE]
            if self._editing_target_index is not None:
                self._targets[self._editing_target_index] = self._working_target
                self._editing_target_index = None
            else:
                self._targets.append(self._working_target)
            self._working_target = {}
            return self.async_show_form(
                step_id=STEP_TARGET_MORE,
                data_schema=_schema_target_more(self),  # type: ignore[arg-type]
                description_placeholders=self._ph_targets(),
            )

        return self.async_show_form(
            step_id=STEP_MATCH_MODE,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MATCH_MODE, default=self._working_target.get(CONF_MATCH_MODE, "all")): selector(
                        {"select": {"options": [{"value": "all", "label": "Require all conditions"}, {"value": "any", "label": "Require any condition"}]}}
                    )
                }
            ),
        )

    async def async_step_target_more(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            nxt = user_input["next"]
            if nxt.startswith("__header_"):
                return self.async_show_form(
                    step_id=STEP_TARGET_MORE,
                    data_schema=_schema_target_more(self),  # type: ignore[arg-type]
                    description_placeholders=self._ph_targets(),
                )
            if nxt.startswith("edit__"):
                try:
                    idx = int(nxt.split("__", 1)[1])
                except ValueError:
                    idx = -1
                if 0 <= idx < len(self._targets):
                    self._editing_target_index = idx
                    self._working_target = copy.deepcopy(self._targets[idx])
                    return self.async_show_form(
                        step_id=STEP_COND_MORE,
                        data_schema=_schema_condition_more(self),  # type: ignore[arg-type]
                        description_placeholders=_placeholders_conditions(self),  # type: ignore[arg-type]
                    )
                return self.async_show_form(
                    step_id=STEP_TARGET_MORE,
                    data_schema=_schema_target_more(self),  # type: ignore[arg-type]
                    description_placeholders=self._ph_targets(),
                )
            if nxt == "add":
                notify_svcs = self.hass.services.async_services().get("notify", {})
                return self.async_show_form(
                    step_id=STEP_ADD_TARGET,
                    data_schema=vol.Schema(
                        {vol.Required("target_service"): selector({"select": {"options": sorted(notify_svcs), "custom_value": True}})}
                    ),
                    description_placeholders=self._ph_targets(),
                )
            if nxt == "edit":
                return await self.async_step_select_target_to_edit()
            if nxt == "remove":
                return await self.async_step_select_target_to_remove()
            if nxt == "done":
                return self.async_show_form(step_id=STEP_ORDER_TARGETS, data_schema=_schema_order_targets(self))  # type: ignore[arg-type]

        return self.async_show_form(
            step_id=STEP_TARGET_MORE,
            data_schema=_schema_target_more(self),  # type: ignore[arg-type]
            description_placeholders=self._ph_targets(),
        )

    async def async_step_select_target_to_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            selected = user_input["target"]
            index = targets.index(selected)
            self._editing_target_index = index
            self._working_target = copy.deepcopy(self._targets[index])
            return self.async_show_form(
                step_id=STEP_COND_MORE,
                data_schema=_schema_condition_more(self),  # type: ignore[arg-type]
                description_placeholders=_placeholders_conditions(self),  # type: ignore[arg-type]
            )
        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_EDIT,
            data_schema=vol.Schema({vol.Required("target"): selector({"select": {"options": targets}})}),
            description_placeholders=self._ph_targets(),
        )

    async def async_step_select_target_to_remove(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        targets = [t[KEY_SERVICE] for t in self._targets]
        if user_input:
            to_remove = set(user_input.get("targets", []))
            self._targets = [t for i, t in enumerate(self._targets) if targets[i] not in to_remove]
            return self.async_show_form(step_id=STEP_TARGET_MORE, data_schema=_schema_target_more(self))  # type: ignore[arg-type]
        return self.async_show_form(
            step_id=STEP_SELECT_TARGET_TO_REMOVE,
            data_schema=vol.Schema(
                {vol.Optional("targets", default=[]): selector({"select": {"options": targets, "multiple": True}})}
            ),
            description_placeholders=self._ph_targets(),
        )

    async def async_step_order_targets(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input:
            priority = list(user_input.get("priority", []))
            by_svc = {t[KEY_SERVICE]: t for t in self._targets}
            ordered = [by_svc[s] for s in priority if s in by_svc]
            for t in self._targets:
                if t not in ordered:
                    ordered.append(t)
            self._targets = ordered
            self._data[CONF_TARGETS] = self._targets
            self._data[CONF_PRIORITY] = [t[KEY_SERVICE] for t in self._targets]
            return self.async_show_form(step_id=STEP_CHOOSE_FALLBACK, data_schema=_schema_choose_fallback(self), errors={})  # type: ignore[arg-type]

        return self.async_show_form(step_id=STEP_ORDER_TARGETS, data_schema=_schema_order_targets(self))  # type: ignore[arg-type]

    async def async_step_choose_fallback(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        notify_svcs = self.hass.services.async_services().get("notify", {})
        if user_input:
            fb = user_input["fallback"]
            if fb not in notify_svcs:
                errors["fallback"] = "must_be_notify"
            else:
                self._data[CONF_FALLBACK] = f"notify.{fb}"
                title = self._data.get(CONF_SERVICE_NAME_RAW) or self._data.get("service_name_raw") or ""
                return self.async_create_entry(title=title, data=self._data)

        return self.async_show_form(step_id=STEP_CHOOSE_FALLBACK, data_schema=_schema_choose_fallback(self), errors=errors)  # type: ignore[arg-type]


# Some HA versions look for this module-level factory.
@callback
def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> OptionsFlow:
    return CustomDeviceNotifierOptionsFlowHandler(config_entry)
