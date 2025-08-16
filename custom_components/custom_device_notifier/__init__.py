from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any, Dict, List, Tuple

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    # base config
    CONF_SERVICE_NAME,
    CONF_SERVICE_NAME_RAW,
    CONF_TARGETS,
    CONF_PRIORITY,
    CONF_FALLBACK,
    CONF_MATCH_MODE,
    KEY_SERVICE,
    KEY_CONDITIONS,
    # routing mode
    CONF_ROUTING_MODE,
    ROUTING_CONDITIONAL,
    ROUTING_SMART,
    DEFAULT_ROUTING_MODE,
    # smart select
    CONF_SMART_PC_NOTIFY,
    CONF_SMART_PC_SESSION,
    CONF_SMART_PHONE_ORDER,
    CONF_SMART_MIN_BATTERY,
    CONF_SMART_PHONE_FRESH_S,
    CONF_SMART_PC_FRESH_S,
    CONF_SMART_REQUIRE_AWAKE,
    CONF_SMART_REQUIRE_UNLOCKED,
    CONF_SMART_POLICY,
    SMART_POLICY_PC_FIRST,
    SMART_POLICY_PHONE_IF_PC_UNLOCKED,
    SMART_POLICY_PHONE_FIRST,
    DEFAULT_SMART_MIN_BATTERY,
    DEFAULT_SMART_PHONE_FRESH_S,
    DEFAULT_SMART_PC_FRESH_S,
    DEFAULT_SMART_REQUIRE_AWAKE,
    DEFAULT_SMART_REQUIRE_UNLOCKED,
    DEFAULT_SMART_POLICY,
    # new: phones must be unlocked/awake to be eligible (optional)
    CONF_SMART_REQUIRE_PHONE_UNLOCKED,
    DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED,
)

_LOGGER = logging.getLogger(__name__)

DATA = f"{DOMAIN}.data"
SERVICE_HANDLES = f"{DOMAIN}.service_handles"


def _signal_name(entry_id: str) -> str:
    return f"{DOMAIN}_route_update_{entry_id}"


@dataclass
class EntryRuntime:
    entry: ConfigEntry
    service_name: str  # notify service name (slug)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data.setdefault(DATA, {})
    hass.data.setdefault(SERVICE_HANDLES, {})
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version >= 3:
        return True

    data = dict(entry.data)

    if not data.get(CONF_SERVICE_NAME):
        raw = str(data.get(CONF_SERVICE_NAME_RAW, "") or "")
        try:
            from homeassistant.helpers.text import slugify  # ≥2025.7
        except Exception:
            from homeassistant.util import slugify  # ≤2025.6
        data[CONF_SERVICE_NAME] = slugify(raw) or "custom_notifier"

    data.setdefault(CONF_TARGETS, list(data.get(CONF_TARGETS, [])))
    data.setdefault(CONF_PRIORITY, list(data.get(CONF_PRIORITY, [])))
    data.setdefault(CONF_FALLBACK, str(data.get(CONF_FALLBACK, "") or ""))

    data.setdefault(
        CONF_SMART_REQUIRE_PHONE_UNLOCKED, DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED
    )

    hass.config_entries.async_update_entry(entry, data=data, version=3)
    _LOGGER.info("Migrated %s entry to version 3", DOMAIN)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    slug = entry.data.get(CONF_SERVICE_NAME)
    if not slug:
        _LOGGER.error(
            "Missing %s in entry data; cannot register service", CONF_SERVICE_NAME
        )
        return False

    hass.data[DATA][entry.entry_id] = EntryRuntime(entry=entry, service_name=slug)

    async def _handle_notify(call: ServiceCall) -> None:
        await _route_and_forward(hass, entry, call.data)

    if hass.services.has_service("notify", slug):
        await hass.services.async_remove("notify", slug)

    hass.services.async_register("notify", slug, _handle_notify)
    hass.data[SERVICE_HANDLES][entry.entry_id] = slug

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _LOGGER.info(
        "Registered notify.%s for %s", slug, entry.data.get(CONF_SERVICE_NAME_RAW, slug)
    )
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _LOGGER.debug("Options updated for %s", entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    slug = hass.data[SERVICE_HANDLES].pop(entry.entry_id, None)
    if slug and hass.services.has_service("notify", slug):
        await hass.services.async_remove("notify", slug)

    ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    hass.data[DATA].pop(entry.entry_id, None)
    return ok


async def _route_and_forward(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]
) -> None:
    cfg = _config_view(entry)

    target_service: str = ""
    decision: Dict[str, Any] = {
        "timestamp": dt_util.utcnow().isoformat(),
        "mode": cfg.get(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE),
        "payload_keys": sorted(list(payload.keys())),
    }

    mode = decision["mode"]

    if mode == ROUTING_SMART:
        svc, info = _choose_service_smart(hass, cfg)
        decision.update({"smart": info})
        target_service = svc or ""
    elif mode == ROUTING_CONDITIONAL:
        svc, info = _choose_service_conditional_with_info(hass, cfg)
        decision.update({"conditional": info})
        target_service = svc or ""
    else:
        _LOGGER.warning("Unknown routing mode %r, falling back to conditional", mode)
        svc, info = _choose_service_conditional_with_info(hass, cfg)
        decision.update({"conditional": info})
        target_service = svc or ""

    via = "matched"
    if not target_service:
        fb = cfg.get(CONF_FALLBACK)
        if isinstance(fb, str) and fb:
            target_service = fb
            via = "fallback"
            _LOGGER.debug("Using fallback %s", fb)
        else:
            decision.update({"result": "dropped"})
            async_dispatcher_send(hass, _signal_name(entry.entry_id), decision)
            _LOGGER.warning("No matching target and no fallback; dropping notification")
            return

    clean = dict(payload)
    clean.pop("service", None)
    clean.pop("services", None)

    domain, service = _split_service(target_service)

    own_slug = str(entry.data.get(CONF_SERVICE_NAME) or "")
    if own_slug and domain == "notify" and service == own_slug:
        _LOGGER.warning(
            "Refusing to call notifier onto itself (%s); using fallback if set.",
            target_service,
        )
        fb = cfg.get(CONF_FALLBACK)
        if isinstance(fb, str) and fb and fb != target_service:
            domain, service = _split_service(fb)
            via = "self-recursion-fallback"
        else:
            decision.update({"result": "dropped_self"})
            async_dispatcher_send(hass, _signal_name(entry.entry_id), decision)
            return

    decision.update(
        {
            "result": "forwarded",
            "service_full": f"{domain}.{service}",
            "via": via,
        }
    )
    async_dispatcher_send(hass, _signal_name(entry.entry_id), decision)

    _LOGGER.debug("Forwarding to %s.%s | title=%s", domain, service, clean.get("title"))
    await hass.services.async_call(domain, service, clean, blocking=True)


def _choose_service_conditional_with_info(
    hass: HomeAssistant, cfg: dict[str, Any]
) -> Tuple[str | None, Dict[str, Any]]:
    targets: List[dict[str, Any]] = list(cfg.get(CONF_TARGETS, []))
    info: Dict[str, Any] = {"matched": [], "priority_used": False}
    if not targets:
        return (None, info)

    matched_services: List[str] = []
    for tgt in targets:
        svc = str(tgt.get(KEY_SERVICE) or "")
        conds: List[dict[str, Any]] = list(tgt.get(KEY_CONDITIONS, []))
        mode: str = str(tgt.get(CONF_MATCH_MODE, "all"))
        if svc and _evaluate_conditions(hass, conds, mode):
            matched_services.append(svc)

    info["matched"] = matched_services

    if not matched_services:
        _LOGGER.debug("No conditional target matched")
        return (None, info)

    priority: List[str] = list(cfg.get(CONF_PRIORITY, []))
    if priority:
        for svc in priority:
            if svc in matched_services:
                info["priority_used"] = True
                _LOGGER.debug("Matched by priority: %s", svc)
                return (svc, info)

    for svc in matched_services:
        _LOGGER.debug("Matched by declaration order: %s", svc)
        return (svc, info)

    return (None, info)


def _evaluate_conditions(
    hass: HomeAssistant, conds: list[dict[str, Any]], mode: str
) -> bool:
    if not conds:
        return True
    results: list[bool] = []
    for c in conds:
        entity_id = str(c.get("entity_id") or "")
        op = str(c.get("operator") or "==")
        val = c.get("value")
        ok = _compare_entity(hass, entity_id, op, val)
        results.append(ok)
    return all(results) if mode == "all" else any(results)


def _compare_entity(hass: HomeAssistant, entity_id: str, op: str, value: Any) -> bool:
    st = hass.states.get(entity_id)
    if isinstance(value, str) and value.strip().lower() == "unknown or unavailable":
        if st is None or st.state in ("unknown", "unavailable"):
            return op == "=="
        return op == "!="
    if st is None:
        return False

    s = st.state
    lhs = _as_float(s)
    rhs = _as_float(value)
    if lhs is not None and rhs is not None and op in (">", "<", ">=", "<=", "==", "!="):
        if op == ">":
            return lhs > rhs
        if op == "<":
            return lhs < rhs
        if op == ">=":
            return lhs >= rhs
        if op == "<=":
            return lhs <= rhs
        if op == "==":
            return lhs == rhs
        if op == "!=":
            return lhs != rhs

    lstr = str(s)
    rstr = str(value)
    if op == "==":
        return lstr == rstr
    if op == "!=":
        return lstr != rstr
    _LOGGER.debug("Unknown operator %s for %s", op, entity_id)
    return False


def _as_float(v: Any) -> float | None:
    try:
        return float(str(v))
    except Exception:
        return None


def _choose_service_smart(
    hass: HomeAssistant, cfg: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    pc_service: str | None = cfg.get(CONF_SMART_PC_NOTIFY)
    pc_session: str | None = cfg.get(CONF_SMART_PC_SESSION)
    phone_order: list[str] = list(cfg.get(CONF_SMART_PHONE_ORDER, []))

    min_batt = int(cfg.get(CONF_SMART_MIN_BATTERY, DEFAULT_SMART_MIN_BATTERY))
    phone_fresh = int(cfg.get(CONF_SMART_PHONE_FRESH_S, DEFAULT_SMART_PHONE_FRESH_S))
    pc_fresh = int(cfg.get(CONF_SMART_PC_FRESH_S, DEFAULT_SMART_PC_FRESH_S))
    require_pc_awake = bool(cfg.get(CONF_SMART_REQUIRE_AWAKE, DEFAULT_SMART_REQUIRE_AWAKE))
    require_pc_unlocked = bool(cfg.get(CONF_SMART_REQUIRE_UNLOCKED, DEFAULT_SMART_REQUIRE_UNLOCKED))
    require_phone_unlocked = bool(cfg.get(CONF_SMART_REQUIRE_PHONE_UNLOCKED, DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED))
    policy = cfg.get(CONF_SMART_POLICY, DEFAULT_SMART_POLICY)

    pc_ok, pc_unlocked = _pc_is_eligible(hass, pc_session, pc_fresh, require_pc_awake, require_pc_unlocked)

    unlocked_ok: list[str] = []
    locked_ok: list[str] = []

    for svc in phone_order:
        basic_ok = _phone_is_eligible(
            hass, svc, min_batt, phone_fresh, require_unlocked=False
        )
        if not basic_ok:
            continue

        domain, short = _split_service(svc)
        slug = short[11:] if short.startswith("mobile_app_") else short
        is_unlocked_now = _phone_is_unlocked_awake(hass, slug, phone_fresh)

        if is_unlocked_now:
            unlocked_ok.append(svc)
        else:
            locked_ok.append(svc)

    eligible_phones = unlocked_ok if require_phone_unlocked else (unlocked_ok + locked_ok)

    chosen: str | None = None
    if policy == SMART_POLICY_PC_FIRST:
        if pc_service and pc_ok:
            chosen = pc_service
        else:
            chosen = (unlocked_ok[0] if unlocked_ok else (locked_ok[0] if locked_ok else None))

    elif policy == SMART_POLICY_PHONE_FIRST:
        if unlocked_ok:
            chosen = unlocked_ok[0]
        elif locked_ok:
            chosen = locked_ok[0]
        elif pc_service and pc_ok:
            chosen = pc_service

    elif policy == SMART_POLICY_PHONE_IF_PC_UNLOCKED:
        if pc_unlocked:
            if unlocked_ok:
                chosen = unlocked_ok[0]
            elif locked_ok:
                chosen = locked_ok[0]
            elif pc_service and pc_ok:
                chosen = pc_service
        else:
            if pc_service and pc_ok:
                chosen = pc_service
            else:
                chosen = (unlocked_ok[0] if unlocked_ok else (locked_ok[0] if locked_ok else None))

    else:
        _LOGGER.warning("Unknown smart policy %r; defaulting to PC_FIRST", policy)
        if pc_service and pc_ok:
            chosen = pc_service
        else:
            chosen = (unlocked_ok[0] if unlocked_ok else (locked_ok[0] if locked_ok else None))

    info = {
        "policy": policy,
        "pc_service": pc_service,
        "pc_session": pc_session,
        "pc_ok": pc_ok,
        "pc_unlocked": pc_unlocked,
        "eligible_phones": eligible_phones,
        "eligible_unlocked": unlocked_ok,
        "eligible_locked": locked_ok,
        "min_battery": min_batt,
        "phone_fresh_s": phone_fresh,
        "pc_fresh_s": pc_fresh,
        "require_pc_awake": require_pc_awake,
        "require_pc_unlocked": require_pc_unlocked,
        "require_phone_unlocked": require_phone_unlocked,
    }
    return (chosen, info)


def _pc_is_eligible(
    hass: HomeAssistant,
    session_entity: str | None,
    fresh_s: int,
    require_awake: bool,
    require_unlocked: bool,
) -> tuple[bool, bool]:
    if not session_entity:
        return (False, False)

    st = hass.states.get(session_entity)
    if st is None:
        return (False, False)

    now = dt_util.utcnow()
    fresh_ok = (now - st.last_updated) <= timedelta(seconds=fresh_s)

    state = (st.state or "").lower().strip()
    unlocked = "unlock" in state and "locked" not in state
    awake = _looks_awake(state)

    eligible = (
        fresh_ok and (awake or not require_awake) and (unlocked or not require_unlocked)
    )
    _LOGGER.debug(
        "PC session %s | state=%s fresh_ok=%s awake=%s unlocked=%s eligible=%s",
        session_entity,
        st.state,
        fresh_ok,
        awake,
        unlocked,
        eligible,
    )
    return (eligible, unlocked)


def _looks_awake(state: str) -> bool:
    s = state.lower()
    if any(k in s for k in ("awake", "active", "online", "available")):
        return True
    if any(
        k in s for k in ("asleep", "sleep", "idle", "suspended", "hibernate", "offline")
    ):
        return False
    return True


def _phone_is_unlocked_awake(hass: HomeAssistant, slug: str, fresh_s: int) -> bool:
    """Return True if we have any fresh signal that the phone is unlocked/awake."""
    candidates = [
        f"binary_sensor.{slug}_interactive",
        f"sensor.{slug}_interactive",
        f"binary_sensor.{slug}_is_interactive",
        f"binary_sensor.{slug}_screen_on",
        f"sensor.{slug}_screen_state",
        f"sensor.{slug}_display_state",
        f"sensor.{slug}_keyguard",
        f"sensor.{slug}_lock_state",
        f"binary_sensor.{slug}_lock",
        f"binary_sensor.{slug}_awake",
        f"sensor.{slug}_awake",
    ]
    now = dt_util.utcnow()
    for ent_id in candidates:
        st = hass.states.get(ent_id)
        if st is None:
            continue
        if (now - st.last_updated) > timedelta(seconds=fresh_s):
            continue
        val = str(st.state or "").strip().lower()
        if val in ("on", "true", "unlocked", "awake", "interactive", "screen_on"):
            return True
        if "unlock" in val and "locked" not in val:
            return True
        if ent_id.endswith("_lock") and val in ("off", "false"):
            return True
    return False


def _phone_is_eligible(
    hass: HomeAssistant,
    notify_service: str,
    min_batt: int,
    fresh_s: int,
    *,
    require_unlocked: bool = False,
) -> bool:
    """Battery + freshness + not-shutdown + optional 'usable_for_notify' gate."""
    domain, svc = _split_service(notify_service)
    if domain != "notify":
        return False

    slug = svc[11:] if svc.startswith("mobile_app_") else svc

    usable = hass.states.get(f"binary_sensor.{slug}_usable_for_notify")
    if usable is not None:
        usable_val = str(usable.state or "").lower()
        if usable_val in ("off", "false", "unavailable", "unknown"):
            _LOGGER.debug("Phone %s | blocked by usable_for_notify=%s", notify_service, usable.state)
            return False

    cand_batt = [f"sensor.{slug}_battery_level", f"sensor.{slug}_battery"]
    batt_ok = True
    for ent_id in cand_batt:
        st = hass.states.get(ent_id)
        if st is None:
            continue
        batt_val = _as_float(st.state)  # <- renamed to avoid mypy str/float union confusion
        if batt_val is not None:
            batt_ok = batt_val >= float(min_batt)
            break

    cand_fresh = [
        f"sensor.{slug}_last_update_trigger",
        f"sensor.{slug}_last_update",
        f"device_tracker.{slug}",
    ]
    now = dt_util.utcnow()
    fresh_ok_any = False
    shutdown_recent = False
    for ent_id in cand_fresh:
        st = hass.states.get(ent_id)
        if st is None:
            continue
        if (now - st.last_updated) <= timedelta(seconds=fresh_s):
            fresh_ok_any = True
            if (
                ent_id.endswith("_last_update_trigger")
                and str(st.state).strip() == "android.intent.action.ACTION_SHUTDOWN"
            ):
                shutdown_recent = True
            break

    if shutdown_recent:
        _LOGGER.debug("Phone %s | rejected due to recent ACTION_SHUTDOWN", notify_service)
        return False

    if not (batt_ok and fresh_ok_any):
        _LOGGER.debug(
            "Phone %s | batt_ok=%s (min=%s) fresh_ok=%s",
            notify_service,
            batt_ok,
            min_batt,
            fresh_ok_any,
        )
        return False

    if require_unlocked:
        if not _phone_is_unlocked_awake(hass, slug, fresh_s):
            _LOGGER.debug("Phone %s | rejected (unlock/interactive required)", notify_service)
            return False

    return True


def _split_service(full: str) -> tuple[str, str]:
    if "." not in full:
        return ("notify", full)
    d, s = full.split(".", 1)
    return (d, s)


def _config_view(entry: ConfigEntry) -> dict[str, Any]:
    cfg = dict(entry.data)
    cfg.update(entry.options or {})
    return cfg
