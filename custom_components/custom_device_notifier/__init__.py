from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any, Dict, List, Optional, Set, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
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
    KEY_MATCH,
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
    # phones must be interactive/unlocked when True
    CONF_SMART_REQUIRE_PHONE_UNLOCKED,
    DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED,
)

_LOGGER = logging.getLogger(__name__)

DATA = f"{DOMAIN}.data"
SERVICE_HANDLES = f"{DOMAIN}.service_handles"


def _signal_name(entry_id: str) -> str:
    """Dispatcher signal used to publish routing decisions (and previews)."""
    return f"{DOMAIN}_route_update_{entry_id}"


@dataclass
class EntryRuntime:
    entry: ConfigEntry
    service_name: str  # notify service name (slug)
    preview: Optional["PreviewManager"] = None  # live preview publisher


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
    """Register notify.<slug>, forward sensor platform, and start live preview."""
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

    # start the live preview publisher (proactive)
    pm = PreviewManager(hass, entry)
    await pm.async_start()
    rt.preview = pm

    # Register proper unload cleanup (expects a callable that returns a coroutine or None)
    async def _stop_preview() -> None:
        await pm.async_stop()

    entry.async_on_unload(_stop_preview)

    # Keep your options-listener registration
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _LOGGER.info(
        "Registered notify.%s for %s", slug, entry.data.get(CONF_SERVICE_NAME_RAW, slug)
    )
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # Rebuild preview watchers/timer on options change
    rt: EntryRuntime | None = hass.data[DATA].get(entry.entry_id)
    if rt and rt.preview:
        await rt.preview.async_rebuild()
    _LOGGER.debug("Options updated for %s", entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down notify service, preview manager, and sensor platform."""
    rt: EntryRuntime | None = hass.data[DATA].pop(entry.entry_id, None)
    if rt and rt.preview:
        await rt.preview.async_stop()

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
            ok_fb = True
            # Only use a mobile_app fallback if that phone is actually eligible
            if fb.startswith("notify.mobile_app_"):
                min_batt = int(
                    cfg.get(CONF_SMART_MIN_BATTERY, DEFAULT_SMART_MIN_BATTERY)
                )
                phone_fresh = int(
                    cfg.get(CONF_SMART_PHONE_FRESH_S, DEFAULT_SMART_PHONE_FRESH_S)
                )
                require_unlocked = bool(
                    cfg.get(
                        CONF_SMART_REQUIRE_PHONE_UNLOCKED,
                        DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED,
                    )
                )
                ok_fb = _phone_is_eligible(
                    hass, fb, min_batt, phone_fresh, require_unlocked=require_unlocked
                )
            if ok_fb:
                target_service = fb
                via = "fallback"
            else:
                decision.update({"result": "dropped"})
                async_dispatcher_send(hass, _signal_name(entry.entry_id), decision)
                _LOGGER.warning("Fallback %s not eligible; dropping notification", fb)
                return
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
) -> tuple[str | None, dict[str, Any]]:
    targets: List[dict[str, Any]] = list(cfg.get(CONF_TARGETS, []))
    info: Dict[str, Any] = {"matched": [], "priority_used": False}
    if not targets:
        return (None, info)

    matched_services: List[str] = []
    for tgt in targets:
        svc = str(tgt.get(KEY_SERVICE) or "")
        conds: List[dict[str, Any]] = list(tgt.get(KEY_CONDITIONS, []))
        mode: str = str(tgt.get(CONF_MATCH_MODE, tgt.get(KEY_MATCH, "all")))
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
    domain, svc = _split_service(notify_service)
    if domain != "notify":
        return False

    slug = svc[11:] if svc.startswith("mobile_app_") else svc

    # battery gate (raw)
    batt_ok = True
    for ent_id in (f"sensor.{slug}_battery_level", f"sensor.{slug}_battery"):
        st = hass.states.get(ent_id)
        if st is None:
            continue
        try:
            batt_ok = float(str(st.state)) >= float(min_batt)
        except Exception:
            pass
        break

    now = dt_util.utcnow()
    fresh_ok_any = False
    shutdown_recent = False

    # freshness & shutdown from raw mobile_app/state entities
    for ent_id in (
        f"sensor.{slug}_last_update_trigger",
        f"sensor.{slug}_last_update",
        f"device_tracker.{slug}",
    ):
        st = hass.states.get(ent_id)
        if st and (now - st.last_updated) <= timedelta(seconds=fresh_s):
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

    if require_unlocked:
        if not _phone_is_unlocked_awake(hass, slug, fresh_s):
            _LOGGER.debug(
                "Phone %s | rejected (locked or not interactive)", notify_service
            )
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

    # If the session explicitly indicates Unlocked/Active, accept even if timestamp is a bit stale.
    if not fresh_ok and (unlocked or awake):
        fresh_ok = True

    eligible = (
        fresh_ok and (awake or not require_awake) and (unlocked or not require_unlocked)
    )

    _LOGGER.debug(
        "PC session %s | state=%s fresh_ok=%s awake=%s unlocked=%s eligible=%s",
        session_entity,
        st.state if st else None,
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


# ───────────────────────── live preview manager (async) ─────────────────────────


_PREVIEW_INTERVAL = timedelta(seconds=5)  # periodic reevaluation for freshness windows


class PreviewManager:
    """Continuously evaluate and publish a 'preview' routing decision via dispatcher."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._unsub_entities: Optional[Callable[[], None]] = None
        self._unsub_timer: Optional[Callable[[], None]] = None

    async def async_start(self) -> None:
        await self._setup_listeners()
        # kick an immediate preview
        await self._publish_preview()

    async def async_rebuild(self) -> None:
        await self.async_stop()
        await self.async_start()

    async def async_stop(self) -> None:
        if self._unsub_entities:
            self._unsub_entities()
            self._unsub_entities = None
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    async def _setup_listeners(self) -> None:
        cfg = _config_view(self.entry)
        entities = self._collect_entities(cfg)

        # state-change driven updates
        if entities:
            self._unsub_entities = async_track_state_change_event(
                self.hass, list(entities), self._on_states_changed
            )

        # periodic freshness guard
        self._unsub_timer = async_track_time_interval(
            self.hass, self._on_timer, _PREVIEW_INTERVAL
        )

    def _collect_entities(self, cfg: dict[str, Any]) -> Set[str]:
        watch: Set[str] = set()
        mode = cfg.get(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE)

        if mode == ROUTING_CONDITIONAL:
            for tgt in cfg.get(CONF_TARGETS, []) or []:
                for c in tgt.get(KEY_CONDITIONS, []) or []:
                    ent = str(c.get("entity_id") or "")
                    if ent:
                        watch.add(ent)

        elif mode == ROUTING_SMART:
            # PC session
            pc_session = cfg.get(CONF_SMART_PC_SESSION)
            if isinstance(pc_session, str) and pc_session:
                watch.add(pc_session)

            # All phone candidates from the priority list (raw signals only)
            for full in list(cfg.get(CONF_SMART_PHONE_ORDER, []) or []):
                slug = _service_slug(full)
                # battery (raw)
                if self.hass.states.get(f"sensor.{slug}_battery_level") is not None:
                    watch.add(f"sensor.{slug}_battery_level")
                if self.hass.states.get(f"sensor.{slug}_battery") is not None:
                    watch.add(f"sensor.{slug}_battery")
                # freshness & shutdown (raw)
                if (
                    self.hass.states.get(f"sensor.{slug}_last_update_trigger")
                    is not None
                ):
                    watch.add(f"sensor.{slug}_last_update_trigger")
                if self.hass.states.get(f"sensor.{slug}_last_update") is not None:
                    watch.add(f"sensor.{slug}_last_update")
                if self.hass.states.get(f"device_tracker.{slug}") is not None:
                    watch.add(f"device_tracker.{slug}")
                # locks / interactive / awake (raw)
                for eid in (
                    f"binary_sensor.{slug}_device_locked",
                    f"binary_sensor.{slug}_locked",
                    f"sensor.{slug}_keyguard",
                    f"sensor.{slug}_lock_state",
                    f"binary_sensor.{slug}_lock",
                    f"binary_sensor.{slug}_interactive",
                    f"sensor.{slug}_interactive",
                    f"binary_sensor.{slug}_is_interactive",
                    f"binary_sensor.{slug}_screen_on",
                    f"sensor.{slug}_screen_state",
                    f"sensor.{slug}_display_state",
                    f"binary_sensor.{slug}_awake",
                    f"sensor.{slug}_awake",
                ):
                    if self.hass.states.get(eid) is not None:
                        watch.add(eid)

        # only keep ones that exist (avoid noisy listeners)
        existing = {e for e in watch if self.hass.states.get(e) is not None}
        return existing

    @callback
    def _on_states_changed(self, _event) -> None:
        # schedule async evaluation on loop
        self.hass.async_create_task(self._publish_preview())

    @callback
    def _on_timer(self, _now) -> None:
        self.hass.async_create_task(self._publish_preview())

    async def _publish_preview(self) -> None:
        """Evaluate using the SAME logic as routing, and publish via dispatcher with via='preview'."""
        cfg = _config_view(self.entry)
        mode = cfg.get(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE)

        chosen: Optional[str] = None
        decision: Dict[str, Any] = {
            "timestamp": dt_util.utcnow().isoformat(),
            "mode": mode,
            "payload_keys": [],  # preview has no payload
            "via": "preview",
        }

        if mode == ROUTING_SMART:
            chosen, info = _choose_service_smart(self.hass, cfg)
            decision["smart"] = info
        else:
            chosen, info = _choose_service_conditional_with_info(self.hass, cfg)
            decision["conditional"] = info

        if not chosen:
            fb = cfg.get(CONF_FALLBACK)
            if isinstance(fb, str) and fb:
                use_fb = True
                if fb.startswith("notify.mobile_app_"):
                    min_batt = int(
                        cfg.get(CONF_SMART_MIN_BATTERY, DEFAULT_SMART_MIN_BATTERY)
                    )
                    phone_fresh = int(
                        cfg.get(CONF_SMART_PHONE_FRESH_S, DEFAULT_SMART_PHONE_FRESH_S)
                    )
                    require_unlocked = bool(
                        cfg.get(
                            CONF_SMART_REQUIRE_PHONE_UNLOCKED,
                            DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED,
                        )
                    )
                    use_fb = _phone_is_eligible(
                        self.hass,
                        fb,
                        min_batt,
                        phone_fresh,
                        require_unlocked=require_unlocked,
                    )
                if use_fb:
                    chosen = fb
                    decision["via"] = "preview-fallback"

        if chosen:
            domain, service = _split_service(chosen)
            decision.update(
                {"result": "forwarded", "service_full": f"{domain}.{service}"}
            )
        else:
            decision.update({"result": "dropped", "service_full": None})

        async_dispatcher_send(self.hass, _signal_name(self.entry.entry_id), decision)
