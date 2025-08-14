from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any, Callable, Iterable

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry as er
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
)

_LOGGER = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Storage
# ──────────────────────────────────────────────────────────────────────────────

DATA = f"{DOMAIN}.data"
SERVICE_HANDLES = f"{DOMAIN}.service_handles"


@dataclass
class EntryRuntime:
    entry: ConfigEntry
    service_name: str  # notify service name (slug)


# ──────────────────────────────────────────────────────────────────────────────
# HA entry points
# ──────────────────────────────────────────────────────────────────────────────

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """YAML not supported; everything is config-entry based."""
    hass.data.setdefault(DOMAIN, {})
    hass.data.setdefault(DATA, {})
    hass.data.setdefault(SERVICE_HANDLES, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from UI config entry."""
    slug = entry.data.get(CONF_SERVICE_NAME)
    if not slug:
        _LOGGER.error("Missing %s in entry data; cannot register service", CONF_SERVICE_NAME)
        return False

    # Remember runtime data
    hass.data[DATA][entry.entry_id] = EntryRuntime(entry=entry, service_name=slug)

    # Register the notify.<slug> service
    async def _handle_notify(call: ServiceCall) -> None:
        await _route_and_forward(hass, entry, call.data)

    # If the service already exists (reload), remove and re-add to avoid stacking handlers
    if hass.services.has_service("notify", slug):
        await hass.services.async_remove("notify", slug)

    hass.services.async_register("notify", slug, _handle_notify)
    hass.data[SERVICE_HANDLES][entry.entry_id] = slug

    # Watch for options updates
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _LOGGER.info("Registered notify.%s for %s", slug, entry.data.get(CONF_SERVICE_NAME_RAW, slug))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to options updates. We keep the same service name; handler reads new options each call."""
    _LOGGER.debug("Options updated for %s", entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Clean up on removal."""
    # Remove service
    slug = hass.data[SERVICE_HANDLES].pop(entry.entry_id, None)
    if slug and hass.services.has_service("notify", slug):
        await hass.services.async_remove("notify", slug)

    # Drop runtime
    hass.data[DATA].pop(entry.entry_id, None)
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Core routing
# ──────────────────────────────────────────────────────────────────────────────

async def _route_and_forward(hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]) -> None:
    """Decide a target notify service and forward the payload."""
    cfg = _config_view(entry)

    # choose service according to routing mode
    target_service: str | None
    mode = cfg.get(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE)
    if mode == ROUTING_SMART:
        target_service = _choose_service_smart(hass, cfg)
    else:
        target_service = _choose_service_conditional(hass, cfg)

    if not target_service:
        # If everything fails, try configured fallback; else bail
        fb = cfg.get(CONF_FALLBACK)
        if fb:
            target_service = fb
            _LOGGER.debug("Using fallback %s", fb)
        else:
            _LOGGER.warning("No matching target and no fallback; dropping notification")
            return

    # Strip our own keys if a caller sent them (defensive)
    clean = dict(payload)
    clean.pop("service", None)
    clean.pop("services", None)

    domain, service = _split_service(target_service)
    _LOGGER.debug("Forwarding to %s.%s | title=%s", domain, service, clean.get("title"))
    await hass.services.async_call(domain, service, clean, blocking=False)


# ──────────────────────────────────────────────────────────────────────────────
# Conditional mode (existing behavior)
# ──────────────────────────────────────────────────────────────────────────────

def _choose_service_conditional(hass: HomeAssistant, cfg: dict[str, Any]) -> str | None:
    """Evaluate targets; pick the first matching by global priority, else None."""
    targets: list[dict[str, Any]] = list(cfg.get(CONF_TARGETS, []))
    if not targets:
        return None

    # Gather matches
    matched_services: set[str] = set()
    for tgt in targets:
        svc: str = tgt.get(KEY_SERVICE)
        conds: list[dict[str, Any]] = tgt.get(KEY_CONDITIONS, [])
        mode: str = tgt.get(CONF_MATCH_MODE, "all")
        if _evaluate_conditions(hass, conds, mode):
            matched_services.add(svc)

    if not matched_services:
        _LOGGER.debug("No conditional target matched")
        return None

    # Choose by priority if provided
    priority: list[str] = list(cfg.get(CONF_PRIORITY, []))
    if priority:
        for svc in priority:
            if svc in matched_services:
                _LOGGER.debug("Matched by priority: %s", svc)
                return svc

    # Otherwise, fall back to first matched in declaration order
    for tgt in targets:
        if tgt.get(KEY_SERVICE) in matched_services:
            svc = tgt.get(KEY_SERVICE)
            _LOGGER.debug("Matched by declaration order: %s", svc)
            return svc

    return None


def _evaluate_conditions(hass: HomeAssistant, conds: list[dict[str, Any]], mode: str) -> bool:
    if not conds:
        # No conditions means "always matches"
        return True

    results: list[bool] = []
    for c in conds:
        entity_id = c.get("entity_id")
        op = c.get("operator") or "=="
        val = c.get("value")
        ok = _compare_entity(hass, entity_id, op, val)
        results.append(ok)

    return all(results) if mode == "all" else any(results)


def _compare_entity(hass: HomeAssistant, entity_id: str, op: str, value: Any) -> bool:
    st = hass.states.get(entity_id)
    # Special matcher: "unknown or unavailable"
    if isinstance(value, str) and value.strip().lower() == "unknown or unavailable":
        if st is None or st.state in ("unknown", "unavailable"):
            return op == "=="  # equal matches the special case
        return op == "!="

    # If entity missing
    if st is None:
        return False

    # Try numeric comparison if both look numeric
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

    # Fallback to string compare
    lstr = str(s)
    rstr = str(value)
    if op == "==":
        return lstr == rstr
    if op == "!=":
        return lstr != rstr

    # Unknown operator → don't match
    _LOGGER.debug("Unknown operator %s for %s", op, entity_id)
    return False


def _as_float(v: Any) -> float | None:
    try:
        return float(str(v))
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Smart Select mode
# ──────────────────────────────────────────────────────────────────────────────

def _choose_service_smart(hass: HomeAssistant, cfg: dict[str, Any]) -> str | None:
    """PC/Phone policy chooser."""
    pc_service: str | None = cfg.get(CONF_SMART_PC_NOTIFY)
    pc_session: str | None = cfg.get(CONF_SMART_PC_SESSION)
    phone_order: list[str] = list(cfg.get(CONF_SMART_PHONE_ORDER, []))

    min_batt = int(cfg.get(CONF_SMART_MIN_BATTERY, DEFAULT_SMART_MIN_BATTERY))
    phone_fresh = int(cfg.get(CONF_SMART_PHONE_FRESH_S, DEFAULT_SMART_PHONE_FRESH_S))
    pc_fresh = int(cfg.get(CONF_SMART_PC_FRESH_S, DEFAULT_SMART_PC_FRESH_S))
    require_awake = bool(cfg.get(CONF_SMART_REQUIRE_AWAKE, DEFAULT_SMART_REQUIRE_AWAKE))
    require_unlocked = bool(cfg.get(CONF_SMART_REQUIRE_UNLOCKED, DEFAULT_SMART_REQUIRE_UNLOCKED))
    policy = cfg.get(CONF_SMART_POLICY, DEFAULT_SMART_POLICY)

    pc_ok, pc_unlocked = _pc_is_eligible(hass, pc_session, pc_fresh, require_awake, require_unlocked)

    def first_ok_phone() -> str | None:
        for svc in phone_order:
            if _phone_is_eligible(hass, svc, min_batt, phone_fresh):
                return svc
        return None

    if policy == SMART_POLICY_PC_FIRST:
        if pc_service and pc_ok:
            return pc_service
        return first_ok_phone()

    if policy == SMART_POLICY_PHONE_FIRST:
        svc = first_ok_phone()
        if svc:
            return svc
        if pc_service and pc_ok:
            return pc_service
        return None

    # SMART_POLICY_PHONE_IF_PC_UNLOCKED
    if pc_unlocked:
        svc = first_ok_phone()
        if svc:
            return svc
        # unlocked but no good phone → still allow PC if otherwise OK
        if pc_service and pc_ok:
            return pc_service
        return None
    # PC not unlocked → go to PC if OK; else phones
    if pc_service and pc_ok:
        return pc_service
    return first_ok_phone()


def _pc_is_eligible(
    hass: HomeAssistant,
    session_entity: str | None,
    fresh_s: int,
    require_awake: bool,
    require_unlocked: bool,
) -> tuple[bool, bool]:
    """Return (eligible, unlocked_flag)."""
    if not session_entity:
        return (False, False)

    st = hass.states.get(session_entity)
    if st is None:
        return (False, False)

    # recency
    now = dt_util.utcnow()
    fresh_ok = (now - st.last_updated) <= timedelta(seconds=fresh_s)

    state = (st.state or "").lower().strip()
    unlocked = "unlock" in state and "locked" not in state  # "unlocked"
    awake = _looks_awake(state)

    eligible = fresh_ok and (awake or not require_awake) and (unlocked or not require_unlocked)
    _LOGGER.debug(
        "PC session %s | state=%s fresh_ok=%s awake=%s unlocked=%s eligible=%s",
        session_entity, st.state, fresh_ok, awake, unlocked, eligible
    )
    return (eligible, unlocked)


def _looks_awake(state: str) -> bool:
    s = state.lower()
    if any(k in s for k in ("awake", "active", "online", "available")):
        return True
    if any(k in s for k in ("asleep", "sleep", "idle", "suspended", "hibernate", "offline")):
        return False
    # Unknown text → assume awake to avoid being too strict
    return True


def _phone_is_eligible(hass: HomeAssistant, notify_service: str, min_batt: int, fresh_s: int) -> bool:
    """Heuristic using mobile_app naming to find battery + freshness signals."""
    domain, svc = _split_service(notify_service)
    if domain != "notify":
        return False

    slug = svc  # e.g. "mobile_app_pixel_7"
    if slug.startswith("mobile_app_"):
        slug = slug[len("mobile_app_") :]

    # Battery sensors commonly used by the mobile_app
    cand_batt = [
        f"sensor.{slug}_battery_level",
        f"sensor.{slug}_battery",
    ]
    batt_ok = True  # if we cannot find a battery sensor, don't block
    for ent_id in cand_batt:
        st = hass.states.get(ent_id)
        if st is None:
            continue
        val = _as_float(st.state)
        if val is not None:
            batt_ok = val >= float(min_batt)
            break

    # Freshness signals (pick the freshest)
    cand_fresh: list[str] = [
        f"sensor.{slug}_last_update_trigger",  # android
        f"sensor.{slug}_last_update",          # sometimes exists
        f"device_tracker.{slug}",              # device tracker updates often
    ]
    now = dt_util.utcnow()
    fresh_ok_any = False
    for ent_id in cand_fresh:
        st = hass.states.get(ent_id)
        if st is None:
            continue
        if (now - st.last_updated) <= timedelta(seconds=fresh_s):
            fresh_ok_any = True
            break

    _LOGGER.debug(
        "Phone %s | batt_ok=%s (min=%s) fresh_ok=%s",
        notify_service, batt_ok, min_batt, fresh_ok_any
    )
    return batt_ok and fresh_ok_any


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _split_service(full: str) -> tuple[str, str]:
    """'notify.mobile_app_x' -> ('notify', 'mobile_app_x')."""
    if "." not in full:
        # be forgiving
        return ("notify", full)
    d, s = full.split(".", 1)
    return (d, s)


def _config_view(entry: ConfigEntry) -> dict[str, Any]:
    """Options override data."""
    cfg = dict(entry.data)
    cfg.update(entry.options or {})
    return cfg
