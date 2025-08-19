from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any, Callable, Dict, List, Optional, Set

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
    CONF_SMART_POLICY,
    SMART_POLICY_PC_FIRST,
    SMART_POLICY_PHONE_IF_PC_UNLOCKED,
    SMART_POLICY_PHONE_FIRST,
    DEFAULT_SMART_MIN_BATTERY,
    DEFAULT_SMART_PHONE_FRESH_S,
    DEFAULT_SMART_PC_FRESH_S,
    DEFAULT_SMART_REQUIRE_AWAKE,
    DEFAULT_SMART_POLICY,
    # kept for options compat, but we hard-require phone unlocked anyway
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

    # still set for options compat; not used for the decision anymore
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

    # proper unload cleanup
    async def _stop_preview() -> None:
        await pm.async_stop()

    entry.async_on_unload(_stop_preview)
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
            # Fallback is always used if nothing else matched.
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
    # special literal: "unknown or unavailable"
    if isinstance(value, str) and value.strip().lower() == "unknown or unavailable":
        if st is None or (st.state in ("unknown", "unavailable")):
            return op == "=="
        return op == "!="
    if st is None:
        return False

    s = st.state
    lhs = _as_float(s)
    rhs = _as_float(value)

    # numeric compare (both sides parse as numbers)
    if lhs is not None and rhs is not None and op in (">", "<", ">=", "<=", "==", "!="):
        left_val = lhs
        right_val = rhs
        if op == ">":
            return left_val > right_val
        if op == "<":
            return left_val < right_val
        if op == ">=":
            return left_val >= right_val
        if op == "<=":
            return left_val <= right_val
        if op == "==":
            return left_val == right_val
        if op == "!=":
            return left_val != right_val

    # string compare fallback
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
    STRICT unlock detection (no 'interactive/screen_on/awake' shortcuts).

    Treat as unlocked ONLY if we see a FRESH (<= fresh_s) explicit unlock signal:
      - binary_sensor.{slug}_device_locked == off
      - binary_sensor.{slug}_lock == off
      - sensor.{slug}_lock_state == 'unlocked'
      - sensor.{slug}_keyguard in {'none', 'keyguard_off'}

    If we also saw a 'locked' signal, the most recent wins.
    If no fresh unlock evidence → locked.
    """
    now = dt_util.utcnow()
    fresh = timedelta(seconds=fresh_s)

    candidates = [
        f"binary_sensor.{slug}_device_locked",
        f"binary_sensor.{slug}_locked",
        f"binary_sensor.{slug}_lock",
        f"sensor.{slug}_lock_state",
        f"sensor.{slug}_keyguard",
    ]

    latest_lock_ts = None
    latest_unlock_ts = None

    for ent_id in candidates:
        st = hass.states.get(ent_id)
        if not st:
            continue
        ts = getattr(st, "last_updated", None)
        if not ts:
            continue

        val = str(st.state or "").strip().lower()
        is_locked = False
        is_unlocked = False

        # binary sensors: on/true => locked, off/false => unlocked
        if (
            ent_id.endswith("_device_locked")
            or ent_id.endswith("_locked")
            or ent_id.endswith("_lock")
        ):
            if val in ("on", "true", "1", "yes", "locked", "screen_locked"):
                is_locked = True
            elif val in ("off", "false", "0", "no"):
                is_unlocked = True

        elif ent_id.endswith("_lock_state"):
            if "unlocked" in val:
                is_unlocked = True
            elif "locked" in val and "unlock" not in val:
                is_locked = True

        elif ent_id.endswith("_keyguard"):
            if val in ("none", "keyguard_off"):
                is_unlocked = True
            else:
                # anything other than explicit "none"/"keyguard_off" counts as locked
                is_locked = True

        if is_locked:
            latest_lock_ts = ts if latest_lock_ts is None else max(latest_lock_ts, ts)
        if is_unlocked and (now - ts) <= fresh:
            latest_unlock_ts = (
                ts if latest_unlock_ts is None else max(latest_unlock_ts, ts)
            )

    if latest_unlock_ts is None:
        return False
    if latest_lock_ts is None:
        return True
    return latest_unlock_ts > latest_lock_ts


def _explain_phone_eligibility(
    hass: HomeAssistant,
    notify_service: str,
    min_batt: int,
    fresh_s: int,
) -> dict[str, Any]:
    """Return a dict explaining each check for debug."""
    _, svc = _split_service(notify_service)
    slug = svc[11:] if svc.startswith("mobile_app_") else svc
    out: Dict[str, Any] = {"service": notify_service, "slug": slug}

    # battery
    batt_ok = True
    batt_val: float | None = None
    for ent_id in (
        f"sensor.{slug}_battery_level",
        f"sensor.{slug}_battery",
        f"sensor.{slug}_battery_percent",
    ):
        st = hass.states.get(ent_id)
        if st is None:
            continue
        try:
            batt_val = float(str(st.state))
            batt_ok = batt_val >= float(min_batt)
        except Exception:
            pass
        break
    out["battery_ok"] = batt_ok
    out["battery_val"] = batt_val

    # freshness
    now = dt_util.utcnow()
    fresh_ok_any = False
    shutdown_recent = False
    for ent_id in (
        f"sensor.{slug}_last_update_trigger",
        f"sensor.{slug}_last_update",
        f"device_tracker.{slug}",
        f"sensor.{slug}_last_notification",
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
    # hints can only make it "fresh", never block
    for eid in (
        f"binary_sensor.{slug}_active_recent",
        f"binary_sensor.{slug}_recent_activity",
        f"binary_sensor.{slug}_fresh",
    ):
        h = hass.states.get(eid)
        if h is not None and str(h.state).lower() in ("on", "true"):
            fresh_ok_any = True
            break
    out["fresh_ok"] = fresh_ok_any
    out["shutdown_recent"] = shutdown_recent

    # unlocked (explicit only; no "screen_on/awake/interactive" shortcuts)
    unlocked = _phone_is_unlocked_awake(hass, slug, fresh_s)
    out["unlocked"] = unlocked

    out["eligible"] = bool(
        batt_ok and fresh_ok_any and (not shutdown_recent) and unlocked
    )
    return out


def _phone_is_eligible(
    hass: HomeAssistant,
    notify_service: str,
    min_batt: int,
    fresh_s: int,
) -> bool:
    """Battery + freshness + not-shutdown + unlocked (strict)."""
    domain, _ = _split_service(notify_service)
    if domain != "notify":
        return False

    expl = _explain_phone_eligibility(hass, notify_service, min_batt, fresh_s)
    return bool(expl["eligible"])


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
) -> tuple[bool, bool]:
    """
    PC is eligible only if session sensor is fresh AND (awake if required) AND unlocked.
    We do NOT consult any custom *_usable_for_notify here.
    """
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

    eligible = fresh_ok and (awake or not require_awake) and unlocked
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
    """
    Policy semantics:

    - PC_FIRST:
        If PC eligible → PC.
        Else → first eligible phone in CONF_SMART_PHONE_ORDER.
    - PHONE_FIRST:
        If any eligible phone → first eligible phone in order.
        Else → PC if eligible.
    - PHONE_IF_PC_UNLOCKED:
        If PC is UNLOCKED (and fresh) → prefer phones (first eligible); if none, choose PC if eligible.
        If PC is locked or not fresh → prefer PC if eligible; else phones (first eligible).
    """
    pc_service: str | None = cfg.get(CONF_SMART_PC_NOTIFY)
    pc_session: str | None = cfg.get(CONF_SMART_PC_SESSION)
    phone_order: list[str] = list(cfg.get(CONF_SMART_PHONE_ORDER, []))

    min_batt = int(cfg.get(CONF_SMART_MIN_BATTERY, DEFAULT_SMART_MIN_BATTERY))
    phone_fresh = int(cfg.get(CONF_SMART_PHONE_FRESH_S, DEFAULT_SMART_PHONE_FRESH_S))
    pc_fresh = int(cfg.get(CONF_SMART_PC_FRESH_S, DEFAULT_SMART_PC_FRESH_S))
    require_pc_awake = bool(
        cfg.get(CONF_SMART_REQUIRE_AWAKE, DEFAULT_SMART_REQUIRE_AWAKE)
    )
    policy = cfg.get(CONF_SMART_POLICY, DEFAULT_SMART_POLICY)

    # we hard-require phone unlocked regardless of the option (kept for compat)
    _ = cfg.get(CONF_SMART_REQUIRE_PHONE_UNLOCKED, DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED)

    # PC eligibility
    pc_ok, pc_unlocked = _pc_is_eligible(hass, pc_session, pc_fresh, require_pc_awake)

    # Phones: compute ordered list that already satisfies battery/freshness/unlocked
    eligible_phones: list[str] = []
    eligibility_by_phone: Dict[str, Any] = {}
    for svc in phone_order:
        expl = _explain_phone_eligibility(hass, svc, min_batt, phone_fresh)
        eligibility_by_phone[svc] = expl
        if expl["eligible"]:
            eligible_phones.append(svc)

    # choose by policy
    chosen: Optional[str] = None
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

    info = {
        "policy": policy,
        "phone_order": phone_order,
        "pc_service": pc_service,
        "pc_session": pc_session,
        "pc_ok": pc_ok,
        "pc_unlocked": pc_unlocked,
        "eligible_phones": eligible_phones,
        "eligibility_by_phone": eligibility_by_phone,
        "min_battery": min_batt,
        "phone_fresh_s": phone_fresh,
        "pc_fresh_s": pc_fresh,
        "require_pc_awake": require_pc_awake,
        "phones_require_unlocked": True,
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
            # PC session only (no custom usable_* signals)
            pc_session = cfg.get(CONF_SMART_PC_SESSION)
            if isinstance(pc_session, str) and pc_session:
                watch.add(pc_session)

            # All phone candidates from the priority list
            for full in list(cfg.get(CONF_SMART_PHONE_ORDER, []) or []):
                slug = _service_slug(full)
                # battery
                for pattern in (
                    f"sensor.{slug}_battery_level",
                    f"sensor.{slug}_battery",
                    f"sensor.{slug}_battery_percent",
                ):
                    if self.hass.states.get(pattern) is not None:
                        watch.add(pattern)
                # freshness & shutdown
                for pattern in (
                    f"sensor.{slug}_last_update_trigger",
                    f"sensor.{slug}_last_update",
                    f"sensor.{slug}_last_notification",
                    f"device_tracker.{slug}",
                ):
                    if self.hass.states.get(pattern) is not None:
                        watch.add(pattern)
                # locks / interactive / awake (raw) + hints (to trigger refresh only)
                for pattern in (
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
                    # hints that should cause preview to refresh quickly
                    f"binary_sensor.{slug}_active_recent",
                    f"binary_sensor.{slug}_recent_activity",
                    f"binary_sensor.{slug}_fresh",
                    f"binary_sensor.{slug}_on_awake",
                ):
                    if self.hass.states.get(pattern) is not None:
                        watch.add(pattern)

        return watch

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
            # show fallback if present (this matches UI expectations for "what would happen now")
            fb = cfg.get(CONF_FALLBACK)
            if isinstance(fb, str) and fb:
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