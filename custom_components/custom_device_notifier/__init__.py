from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any, Dict, List, Tuple, Callable, Optional, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
    async_call_later,
)
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
    # new flag (phones must be interactive/unlocked when True)
    CONF_SMART_REQUIRE_PHONE_UNLOCKED,
    DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED,
)

_LOGGER = logging.getLogger(__name__)

DATA = f"{DOMAIN}.data"
SERVICE_HANDLES = f"{DOMAIN}.service_handles"


def _signal_name(entry_id: str) -> str:
    """Dispatcher signal used to publish routing decisions."""
    return f"{DOMAIN}_route_update_{entry_id}"


@dataclass
class EntryRuntime:
    entry: ConfigEntry
    service_name: str  # notify service name (slug)
    # Live preview wiring (for background re-eval)
    unsub_preview_change: Optional[Callable[[], None]] = None
    unsub_preview_interval: Optional[Callable[[], None]] = None
    _preview_timer_cancel: Optional[Callable[[], None]] = None


# ─────────────────────────── lifecycle ───────────────────────────


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data.setdefault(DATA, {})
    hass.data.setdefault(SERVICE_HANDLES, {})
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older entries → v3, normalize shapes, seed new defaults."""
    if entry.version >= 3:
        return True

    data = dict(entry.data)

    # ensure we have a slug if older entries only had RAW
    if not data.get(CONF_SERVICE_NAME):
        raw = str(data.get(CONF_SERVICE_NAME_RAW, "") or "")
        try:
            from homeassistant.helpers.text import slugify  # ≥2025.7
        except Exception:  # pragma: no cover
            from homeassistant.util import slugify  # ≤2025.6
        data[CONF_SERVICE_NAME] = slugify(raw) or "custom_notifier"

    # normalize container types / missing keys
    data.setdefault(CONF_TARGETS, list(data.get(CONF_TARGETS, [])))
    data.setdefault(CONF_PRIORITY, list(data.get(CONF_PRIORITY, [])))
    data.setdefault(CONF_FALLBACK, str(data.get(CONF_FALLBACK, "") or ""))

    # new flag default (exposed in flow)
    data.setdefault(
        CONF_SMART_REQUIRE_PHONE_UNLOCKED, DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED
    )

    hass.config_entries.async_update_entry(entry, data=data, version=3)
    _LOGGER.info("Migrated %s entry to version 3", DOMAIN)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register notify.<slug>, forward sensor platform, wire background preview."""
    slug = entry.data.get(CONF_SERVICE_NAME)
    if not slug:
        _LOGGER.error(
            "Missing %s in entry data; cannot register service", CONF_SERVICE_NAME
        )
        return False

    rt = EntryRuntime(entry=entry, service_name=slug)
    hass.data[DATA][entry.entry_id] = rt

    async def _handle_notify(call: ServiceCall) -> None:
        await _route_and_forward(hass, entry, call.data)

    # if reloaded, remove stale service first
    if hass.services.has_service("notify", slug):
        hass.services.async_remove("notify", slug)

    hass.services.async_register("notify", slug, _handle_notify)
    hass.data[SERVICE_HANDLES][entry.entry_id] = slug

    # live “current target” sensor platform (subscribes to our dispatcher)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    # Make sure the sensor is added, then wire preview + seed immediately
    await hass.async_block_till_done()
    _rebuild_preview_watchers(hass, rt)
    await _publish_preview_decision(hass, entry, via="seed")

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _LOGGER.info(
        "Registered notify.%s for %s", slug, entry.data.get(CONF_SERVICE_NAME_RAW, slug)
    )
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    _LOGGER.debug("Options updated for %s", entry.entry_id)
    rt = hass.data[DATA].get(entry.entry_id)
    if not isinstance(rt, EntryRuntime):
        return
    # Rewire watchers (entities may have changed) and refresh immediately
    _rebuild_preview_watchers(hass, rt)
    await _publish_preview_decision(hass, entry, via="options")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down notify service, sensor platform, and preview watchers."""
    rt: EntryRuntime | None = hass.data[DATA].pop(entry.entry_id, None)
    if rt:
        if rt.unsub_preview_change:
            rt.unsub_preview_change()
            rt.unsub_preview_change = None
        if rt.unsub_preview_interval:
            rt.unsub_preview_interval()
            rt.unsub_preview_interval = None
        if rt._preview_timer_cancel:
            rt._preview_timer_cancel()
            rt._preview_timer_cancel = None

    slug = hass.data[SERVICE_HANDLES].pop(entry.entry_id, None)
    if slug and hass.services.has_service("notify", slug):
        hass.services.async_remove("notify", slug)

    ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    return ok


# ─────────────────────────── routing entry point ───────────────────────────


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

    # refuse to call ourselves; try fallback if available
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


# ───────────────────────── conditional routing ─────────────────────────


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

    # priority wins if provided; else declaration order
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


# ───────────────────────── smart select routing ─────────────────────────


def _service_slug(full: str) -> str:
    """Return the slug portion from a notify service (strip domain and mobile_app_)."""
    domain, svc = _split_service(full)
    slug = svc
    if slug.startswith("mobile_app_"):
        slug = slug[len("mobile_app_") :]
    return slug


def _phone_is_unlocked_awake(hass: HomeAssistant, slug: str, fresh_s: int) -> bool:
    """
    Decide unlocked vs locked by timestamp precedence.

    - Any 'locked' signal (even stale) blocks unless there's a newer fresh 'unlocked/interactive'.
    - Positive 'unlocked/interactive' must be fresh (<= fresh_s).
    - If no fresh positive and either a locked exists (stale or fresh) or nothing exists, treat as NOT unlocked.
    """
    now = dt_util.utcnow()
    fresh = timedelta(seconds=fresh_s)

    # collect likely "locked" signals
    latest_lock_ts = None
    saw_lock = False
    lock_entities = [
        f"binary_sensor.{slug}_device_locked",
        f"binary_sensor.{slug}_locked",
        f"sensor.{slug}_keyguard",
        f"sensor.{slug}_lock_state",
        f"binary_sensor.{slug}_lock",  # some templates; on == locked
    ]
    for ent_id in lock_entities:
        st = hass.states.get(ent_id)
        if not st:
            continue
        val = str(st.state or "").strip().lower()
        is_locked = val in ("on", "true", "locked", "screen_locked") or (
            "locked" in val and "unlock" not in val
        )
        if is_locked:
            saw_lock = True
            ts = getattr(st, "last_updated", None)
            if ts:
                latest_lock_ts = (
                    ts if latest_lock_ts is None else max(latest_lock_ts, ts)
                )

    # collect fresh positive "interactive/unlocked/awake"
    latest_unlock_ts = None
    saw_fresh_unlock = False
    positive_entities = [
        f"binary_sensor.{slug}_interactive",
        f"sensor.{slug}_interactive",
        f"binary_sensor.{slug}_is_interactive",
        f"binary_sensor.{slug}_screen_on",
        f"sensor.{slug}_screen_state",
        f"sensor.{slug}_display_state",
        f"sensor.{slug}_keyguard",  # "none", "keyguard_off"
        f"sensor.{slug}_lock_state",  # "unlocked"
        f"binary_sensor.{slug}_lock",  # off == unlocked
        f"binary_sensor.{slug}_awake",
        f"sensor.{slug}_awake",
    ]
    for ent_id in positive_entities:
        st = hass.states.get(ent_id)
        if not st:
            continue
        ts = getattr(st, "last_updated", None)
        if not ts or (now - ts) > fresh:
            continue
        val = str(st.state or "").strip().lower()
        is_unlocked = (
            val
            in (
                "on",
                "true",
                "unlocked",
                "awake",
                "interactive",
                "screen_on",
                "none",
                "keyguard_off",
            )
            or (ent_id.endswith("_lock") and val in ("off", "false"))
            or ("unlock" in val and "locked" not in val)
        )
        if is_unlocked:
            saw_fresh_unlock = True
            latest_unlock_ts = (
                ts if latest_unlock_ts is None else max(latest_unlock_ts, ts)
            )

    if saw_lock and not saw_fresh_unlock:
        return False
    if saw_lock and saw_fresh_unlock:
        if latest_lock_ts and latest_unlock_ts and latest_unlock_ts > latest_lock_ts:
            return True
        return False
    return bool(saw_fresh_unlock)


def _phone_is_eligible(
    hass: HomeAssistant,
    notify_service: str,
    min_batt: int,
    fresh_s: int,
    *,
    require_unlocked: bool = False,
) -> bool:
    """Battery + freshness + not-shutdown + optional unlocked/interactive check."""
    domain, svc = _split_service(notify_service)
    if domain != "notify":
        return False

    slug = svc[11:] if svc.startswith("mobile_app_") else svc

    # optional kill switch
    usable = hass.states.get(f"binary_sensor.{slug}_usable_for_notify")
    if usable is not None:
        uval = str(usable.state or "").lower()
        if uval in ("off", "false", "unavailable", "unknown"):
            _LOGGER.debug(
                "Phone %s | blocked by usable_for_notify=%s",
                notify_service,
                usable.state,
            )
            return False

    # battery gate
    cand_batt = [f"sensor.{slug}_battery_level", f"sensor.{slug}_battery"]
    batt_ok = True
    for ent_id in cand_batt:
        st = hass.states.get(ent_id)
        if not st:
            continue
        try:
            batt_val = float(str(st.state))
        except Exception:
            continue
        batt_ok = batt_val >= float(min_batt)
        break

    # freshness + shutdown gate
    now = dt_util.utcnow()
    fresh_ok_any = False
    shutdown_recent = False
    cand_fresh = [
        f"sensor.{slug}_last_update_trigger",
        f"sensor.{slug}_last_update",
        f"device_tracker.{slug}",
    ]
    for ent_id in cand_fresh:
        st = hass.states.get(ent_id)
        if not st:
            continue
        if (now - st.last_updated) <= timedelta(seconds=fresh_s):
            fresh_ok_any = True
            if (
                ent_id.endswith("_last_update_trigger")
                and str(st.state).strip() == "android.intent.action.ACTION_SHUTDOWN"
            ):
                shutdown_recent = True
            break

    if shutdown_recent or not (batt_ok and fresh_ok_any):
        _LOGGER.debug(
            "Phone %s | rejected: shutdown_recent=%s batt_ok=%s fresh_ok=%s",
            notify_service,
            shutdown_recent,
            batt_ok,
            fresh_ok_any,
        )
        return False

    if require_unlocked and not _phone_is_unlocked_awake(hass, slug, fresh_s):
        _LOGGER.debug("Phone %s | rejected (locked or not interactive)", notify_service)
        return False

    return True


def _looks_awake(state: str) -> bool:
    s = state.lower()
    if any(k in s for k in ("awake", "active", "online", "available")):
        return True
    if any(
        k in s for k in ("asleep", "sleep", "idle", "suspended", "hibernate", "offline")
    ):
        return False
    return True


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


def _choose_service_smart(
    hass: HomeAssistant, cfg: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    pc_service: str | None = cfg.get(CONF_SMART_PC_NOTIFY)
    pc_session: str | None = cfg.get(CONF_SMART_PC_SESSION)
    phone_order: list[str] = list(cfg.get(CONF_SMART_PHONE_ORDER, []))

    min_batt = int(cfg.get(CONF_SMART_MIN_BATTERY, DEFAULT_SMART_MIN_BATTERY))
    phone_fresh = int(cfg.get(CONF_SMART_PHONE_FRESH_S, DEFAULT_SMART_PHONE_FRESH_S))
    pc_fresh = int(cfg.get(CONF_SMART_PC_FRESH_S, DEFAULT_SMART_PC_FRESH_S))
    require_pc_awake = bool(
        cfg.get(CONF_SMART_REQUIRE_AWAKE, DEFAULT_SMART_REQUIRE_AWAKE)
    )
    require_pc_unlocked = bool(
        cfg.get(CONF_SMART_REQUIRE_UNLOCKED, DEFAULT_SMART_REQUIRE_UNLOCKED)
    )
    require_phone_unlocked_effective = bool(
        cfg.get(CONF_SMART_REQUIRE_PHONE_UNLOCKED, DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED)
    )
    policy = cfg.get(CONF_SMART_POLICY, DEFAULT_SMART_POLICY)

    pc_ok, pc_unlocked = _pc_is_eligible(
        hass, pc_session, pc_fresh, require_pc_awake, require_pc_unlocked
    )

    eligible_phones: list[str] = []
    for svc in phone_order:
        if _phone_is_eligible(
            hass,
            svc,
            min_batt,
            phone_fresh,
            require_unlocked=require_phone_unlocked_effective,
        ):
            eligible_phones.append(svc)

    chosen: str | None = None
    if policy == SMART_POLICY_PC_FIRST:
        if pc_ok:
            chosen = pc_service
        elif eligible_phones:
            chosen = eligible_phones[0]

    elif policy == SMART_POLICY_PHONE_FIRST:
        if eligible_phones:
            chosen = eligible_phones[0]
        elif pc_ok:
            chosen = pc_service

    elif policy == SMART_POLICY_PHONE_IF_PC_UNLOCKED:
        if pc_unlocked:
            chosen = (
                eligible_phones[0]
                if eligible_phones
                else (pc_service if pc_ok else None)
            )
        else:
            chosen = (
                pc_service
                if pc_ok
                else (eligible_phones[0] if eligible_phones else None)
            )

    else:
        _LOGGER.warning("Unknown smart policy %r; defaulting to PC_FIRST", policy)
        if pc_ok:
            chosen = pc_service
        elif eligible_phones:
            chosen = eligible_phones[0]

    # final guard against stale reads: if a phone was selected, verify again
    if chosen and chosen.startswith("notify.mobile_app_"):
        if not _phone_is_eligible(
            hass,
            chosen,
            min_batt,
            phone_fresh,
            require_unlocked=require_phone_unlocked_effective,
        ):
            _LOGGER.debug(
                "Final guard rejected %s (locked/shutdown/not fresh). Falling back.",
                chosen,
            )
            chosen = None

    info = {
        "policy": policy,
        "pc_service": pc_service,
        "pc_session": pc_session,
        "pc_ok": pc_ok,
        "pc_unlocked": pc_unlocked,
        "eligible_phones": eligible_phones,
        "min_battery": min_batt,
        "phone_fresh_s": phone_fresh,
        "pc_fresh_s": pc_fresh,
        "require_pc_awake": require_pc_awake,
        "require_pc_unlocked": require_pc_unlocked,
        "require_phone_unlocked_effective": require_phone_unlocked_effective,
    }
    return (chosen, info)


# ───────────────────────── utilities ─────────────────────────


def _split_service(full: str) -> tuple[str, str]:
    if "." not in full:
        return ("notify", full)
    d, s = full.split(".", 1)
    return (d, s)


def _config_view(entry: ConfigEntry) -> dict[str, Any]:
    cfg = dict(entry.data)
    cfg.update(entry.options or {})
    return cfg


# ───────────────────────── background preview wiring ─────────────────────────


def _watched_entities(cfg: dict[str, Any]) -> List[str]:
    """Compute the entity_ids that affect routing, for change-triggered preview."""
    mode = cfg.get(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE)
    ents: set[str] = set()

    if mode == ROUTING_CONDITIONAL:
        for tgt in list(cfg.get(CONF_TARGETS, [])):
            for c in list(tgt.get(KEY_CONDITIONS, [])):
                e = str(c.get("entity_id") or "").strip()
                if e:
                    ents.add(e)
        return sorted(ents)

    # SMART mode
    pc_session = str(cfg.get(CONF_SMART_PC_SESSION) or "").strip()
    if pc_session:
        ents.add(pc_session)

    phone_order: Iterable[str] = list(cfg.get(CONF_SMART_PHONE_ORDER, []))
    for full in phone_order:
        slug = _service_slug(full)  # strip domain + mobile_app_
        # Mirrors eligibility checks
        ents.update(
            {
                f"sensor.{slug}_battery_level",
                f"sensor.{slug}_battery",
                f"sensor.{slug}_last_update_trigger",
                f"sensor.{slug}_last_update",
                f"device_tracker.{slug}",
                f"binary_sensor.{slug}_usable_for_notify",
                # interactive/lock/awake signals
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
            }
        )
    return sorted(ents)


def _rebuild_preview_watchers(hass: HomeAssistant, rt: EntryRuntime) -> None:
    """Re-subscribe to state changes relevant to the current config and start periodic refresh."""
    # Clear old subscriptions
    if rt.unsub_preview_change:
        rt.unsub_preview_change()
        rt.unsub_preview_change = None
    if rt.unsub_preview_interval:
        rt.unsub_preview_interval()
        rt.unsub_preview_interval = None
    if rt._preview_timer_cancel:
        rt._preview_timer_cancel()
        rt._preview_timer_cancel = None

    cfg = _config_view(rt.entry)
    entities = _watched_entities(cfg)

    @callback
    def _on_relevant_state_change(event):
        _schedule_preview_publish(hass, rt, delay=0.25)

    if entities:
        rt.unsub_preview_change = async_track_state_change_event(
            hass, entities, _on_relevant_state_change
        )

    # Periodic refresh (freshness windows tick over even without entity events)
    def _interval_cb(now):
        hass.async_create_task(_publish_preview_decision(hass, rt.entry, via="tick"))

    rt.unsub_preview_interval = async_track_time_interval(
        hass, _interval_cb, timedelta(seconds=30)
    )

    # First evaluation will be done by caller (seed/options), but schedule a tiny one too
    _schedule_preview_publish(hass, rt, delay=0.5)


def _schedule_preview_publish(hass: HomeAssistant, rt: EntryRuntime, *, delay: float):
    """Debounced scheduling to avoid flapping on bursty updates."""
    if rt._preview_timer_cancel:
        return  # already scheduled

    def _fire(_now):
        rt._preview_timer_cancel = None
        hass.async_create_task(_publish_preview_decision(hass, rt.entry, via="preview"))

    rt._preview_timer_cancel = async_call_later(hass, delay, _fire)


async def _publish_preview_decision(
    hass: HomeAssistant, entry: ConfigEntry, *, via: str
) -> None:
    """Compute the current best target and publish a synthetic decision for the sensor."""
    cfg = _config_view(entry)
    mode = cfg.get(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE)

    chosen: str | None = None
    info: Dict[str, Any] = {}

    if mode == ROUTING_SMART:
        chosen, info = _choose_service_smart(hass, cfg)
    else:  # ROUTING_CONDITIONAL (or default)
        chosen, info = _choose_service_conditional_with_info(hass, cfg)

    if not chosen:
        fb = cfg.get(CONF_FALLBACK)
        if isinstance(fb, str) and fb:
            chosen = fb
            via = f"{via}-fallback"

    decision: Dict[str, Any] = {
        "timestamp": dt_util.utcnow().isoformat(),
        "mode": mode,
        "payload_keys": [],
        "via": via,
    }
    if mode == ROUTING_SMART:
        decision["smart"] = info
    else:
        decision["conditional"] = info

    if chosen:
        decision.update({"result": "forwarded", "service_full": chosen})
    else:
        decision.update({"result": "dropped"})

    async_dispatcher_send(hass, _signal_name(entry.entry_id), decision)
