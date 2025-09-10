from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any, Callable, Dict, List, Optional, Set

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback, State
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
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
    CONF_SMART_PHONE_UNLOCK_WINDOW_S,
    DEFAULT_SMART_PHONE_UNLOCK_WINDOW_S,
    TTS_OPT_ENABLE,
    TTS_OPT_DEFAULT,
    TTS_OPT_SERVICE,
    TTS_OPT_LANGUAGE,
    MEDIA_ORDER_OPT,
)

from .notify import build_notify_payload  # <-- use helper to preserve nested data

_STORE_VER = 1
_STORE_KEY = f"{DOMAIN}_memory"

_LOGGER = logging.getLogger(__name__)

DATA = f"{DOMAIN}.data"
SERVICE_HANDLES = f"{DOMAIN}.service_handles"

# If not configured in options, we use this sticky unlock memory window (seconds).
DEFAULT_UNLOCK_WINDOW_S = DEFAULT_SMART_PHONE_UNLOCK_WINDOW_S

# In-memory store for last known explicit unlock timestamps per phone slug
_LAST_PHONE_UNLOCK_UTC: Dict[str, datetime] = {}

# New, optional option keys (read as raw strings from entry options)
# - smart_pc_like_services: List[str] of notify services to force as PC-like
# - smart_pc_autodetect: bool to enable auto-detect (screen_lock/non-mobile_app) as PC-like
OPT_PC_LIKE_SERVICES = "smart_pc_like_services"
OPT_PC_AUTODETECT = "smart_pc_autodetect"


def _signal_name(entry_id: str) -> str:
    """Dispatcher signal used to publish routing decisions (and previews)."""
    return f"{DOMAIN}_route_update_{entry_id}"


_STARTUP_GRACE_S = 120  # you can make this an option later if you want
_BOOT_UTC = dt_util.utcnow()


def _is_restored_or_boot_fresh(st: State | None) -> bool:
    """True if the state looks restored at startup (don’t trust freshness/unlock)."""
    if st is None:
        return False
    # Many RestoreEntitys add attributes["restored"]: True
    if isinstance(st.attributes, dict) and st.attributes.get("restored") is True:
        return True
    ts_any = getattr(st, "last_updated", None)
    ts = ts_any if isinstance(ts_any, datetime) else None
    if ts is None:
        return False
    # treat anything 'updated' in the first N seconds after boot as restored
    return (ts - _BOOT_UTC) <= timedelta(seconds=_STARTUP_GRACE_S)


@dataclass
class EntryRuntime:
    entry: ConfigEntry
    service_name: str  # notify service name (slug)
    preview: Optional["PreviewManager"] = None  # live preview publisher


# ─────────────────────────── lifecycle ───────────────────────────


async def _maybe_play_tts(
    hass: HomeAssistant,
    entry: ConfigEntry,
    payload: dict[str, Any],
    cfg: dict[str, Any],
) -> None:
    """If TTS is enabled and requested, call the configured tts.* service."""
    if not cfg.get(TTS_OPT_ENABLE):
        return

    data = payload.get("data") or {}
    # 1) explicit request via data.tts_text
    text = None
    if isinstance(data, dict) and data.get("tts_text"):
        text = str(data["tts_text"])
    # 2) or speak the normal message when 'Send TTS by default' is on
    elif cfg.get(TTS_OPT_DEFAULT):
        msg = payload.get("message") or payload.get("title")
        if msg:
            text = str(msg)

    if not text:
        return

    # Pick a media player: payload override > first in configured order
    mp = data.get("media_player_entity_id")
    if not mp:
        order = cfg.get(MEDIA_ORDER_OPT) or []
        if order:
            mp = order[0]

    if not mp:
        _LOGGER.warning("TTS requested but no media player is configured/ordered.")
        return

    tts_service = cfg.get(TTS_OPT_SERVICE)
    if not tts_service or "." not in tts_service:
        _LOGGER.warning(
            "TTS requested but tts_service option is not set correctly: %r", tts_service
        )
        return

    lang = cfg.get(TTS_OPT_LANGUAGE) or None
    tts_domain, tts_method = tts_service.split(".", 1)

    # tts.speak uses media_player_entity_id; legacy engines (google_translate_say) use entity_id
    svc_data: dict[str, Any] = {"message": text}
    if tts_method == "speak":
        svc_data["media_player_entity_id"] = mp
    else:
        svc_data["entity_id"] = mp
        svc_data["cache"] = False
    if lang:
        svc_data["language"] = lang

    _LOGGER.debug("TTS: calling %s with %s", tts_service, svc_data)
    await hass.services.async_call(tts_domain, tts_method, svc_data, blocking=True)


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data.setdefault(DATA, {})
    hass.data.setdefault(SERVICE_HANDLES, {})
    store = Store(hass, _STORE_VER, _STORE_KEY)
    mem = await store.async_load() or {}
    hass.data[DATA]["store"] = store
    hass.data[DATA]["memory"] = mem

    # Seed sticky unlocks from disk
    for slug, ts_iso in (mem.get("last_phone_unlock", {})).items():
        try:
            _LAST_PHONE_UNLOCK_UTC[slug] = dt_util.parse_datetime(ts_iso)
        except Exception:
            pass
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

    # Register with a permissive schema that preserves nested 'data'
    hass.services.async_register(
        "notify",
        slug,
        _handle_notify,
        schema=vol.Schema(
            {
                vol.Required("message"): vol.Any(str, int, float),
                vol.Optional("title"): vol.Any(str, int, float, None),
                vol.Optional("data"): dict,  # <-- keep nested dict
                vol.Optional("target"): vol.Any(str, [str], None),
                # allow arbitrary extras; HA ignores unknowns for most notify backends
            }
        ),
    )
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
            target_service = fb
            via = "fallback"
            _LOGGER.debug("Using fallback %s", fb)
        else:
            decision.update({"result": "dropped"})
            async_dispatcher_send(hass, _signal_name(entry.entry_id), decision)
            _LOGGER.warning("No matching target and no fallback; dropping notification")
            return

    # Clean raw payload and let notify.py normalize it (keeps nested "data")
    raw = dict(payload)
    raw.pop("service", None)
    raw.pop("services", None)
    out = build_notify_payload(raw)

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
    await _maybe_play_tts(hass, entry, payload, cfg)

    _LOGGER.debug("Forwarding to %s.%s | title=%s", domain, service, out.get("title"))
    await hass.services.async_call(domain, service, out, blocking=True)


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


def _split_service(full: str) -> tuple[str, str]:
    if "." not in full:
        return ("notify", full)
    d, s = full.split(".", 1)
    return (d, s)


def _service_slug(full: str) -> str:
    """Return the slug portion from a notify service (strip domain and mobile_app_)."""
    _, svc = _split_service(full)
    slug = svc
    if slug.startswith("mobile_app_"):
        slug = slug[len("mobile_app_") :]
    return slug


# ---------- PHONE logic (sticky explicit unlock) ----------


def _explicit_unlock_times(
    hass: HomeAssistant, slug: str, fresh_s: int
) -> tuple[Optional[datetime], Optional[datetime], Optional[datetime]]:
    """
    Return (latest_lock_ts, latest_fresh_unlock_ts, latest_any_unlock_ts).

    Only explicit unlocks count:
      - binary_sensor.{slug}_device_locked == off
      - binary_sensor.{slug}_lock == off
      - sensor.{slug}_lock_state == 'unlocked'
      - sensor.{slug}_keyguard in {'none', 'keyguard_off'}
    """
    now_dt = dt_util.utcnow()
    fresh = timedelta(seconds=fresh_s)

    candidates = [
        f"binary_sensor.{slug}_device_locked",
        f"binary_sensor.{slug}_locked",
        f"binary_sensor.{slug}_lock",
        f"sensor.{slug}_lock_state",
        f"sensor.{slug}_keyguard",
    ]

    latest_lock_ts: Optional[datetime] = None
    latest_fresh_unlock_ts: Optional[datetime] = None
    latest_any_unlock_ts: Optional[datetime] = None

    for ent_id in candidates:
        st = hass.states.get(ent_id)
        if not st or _is_restored_or_boot_fresh(st):
            continue
        ts_any = getattr(st, "last_updated", None)
        ts: Optional[datetime] = ts_any if isinstance(ts_any, datetime) else None
        if ts is None:
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
                is_locked = True

        if is_locked:
            latest_lock_ts = ts if latest_lock_ts is None else max(latest_lock_ts, ts)
        if is_unlocked:
            latest_any_unlock_ts = (
                ts if latest_any_unlock_ts is None else max(latest_any_unlock_ts, ts)
            )
            if (now_dt - ts) <= fresh:
                latest_fresh_unlock_ts = (
                    ts
                    if latest_fresh_unlock_ts is None
                    else max(latest_fresh_unlock_ts, ts)
                )

    return (latest_lock_ts, latest_fresh_unlock_ts, latest_any_unlock_ts)


def _phone_is_unlocked_with_sticky(
    hass: HomeAssistant,
    slug: str,
    fresh_s: int,
    unlock_window_s: int,
    *,
    fresh_ok_any: bool,
) -> bool:
    """
    Explicit-unlock with sticky window:

    - Fresh explicit unlock → unlocked (record it).
    - Else if ANY explicit unlock exists (even stale), the phone is fresh overall,
      and there's no newer lock → unlocked (seed memory).
    - Else if we previously saw an unlock within unlock_window_s and no newer lock → unlocked.
    - Else locked.
    """
    latest_lock_ts, latest_fresh_unlock_ts, latest_any_unlock_ts = (
        _explicit_unlock_times(hass, slug, fresh_s)
    )
    now_dt = dt_util.utcnow()

    # Fresh explicit unlock wins
    if latest_fresh_unlock_ts is not None:
        _LAST_PHONE_UNLOCK_UTC[slug] = latest_fresh_unlock_ts
        return True

    # Stale explicit unlock can seed if phone itself is fresh and no newer lock exists
    if fresh_ok_any and latest_any_unlock_ts is not None:
        if latest_lock_ts is None or latest_any_unlock_ts > latest_lock_ts:
            _LAST_PHONE_UNLOCK_UTC[slug] = latest_any_unlock_ts
            return True

    # Sticky memory window
    last_mem = _LAST_PHONE_UNLOCK_UTC.get(slug)
    if last_mem is not None and (now_dt - last_mem) <= timedelta(
        seconds=unlock_window_s
    ):
        if latest_lock_ts is None or last_mem > latest_lock_ts:
            return True

    return False


def _explain_phone_eligibility(
    hass: HomeAssistant,
    notify_service: str,
    min_batt: int,
    fresh_s: int,
    unlock_window_s: int,
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
    now_dt: datetime = dt_util.utcnow()
    fresh_ok_any = False
    shutdown_recent = False
    for ent_id in (
        f"sensor.{slug}_last_update_trigger",
        f"sensor.{slug}_last_update",
        f"device_tracker.{slug}",
        f"sensor.{slug}_last_notification",
    ):
        st = hass.states.get(ent_id)
        if not st or _is_restored_or_boot_fresh(st):
            continue
        ts_any = getattr(st, "last_updated", None)
        ts: Optional[datetime] = ts_any if isinstance(ts_any, datetime) else None
        if ts is None:
            continue
        if (now_dt - ts) <= timedelta(seconds=fresh_s):
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

    # unlocked with sticky logic
    unlocked = _phone_is_unlocked_with_sticky(
        hass, slug, fresh_s, unlock_window_s, fresh_ok_any=fresh_ok_any
    )
    out["unlocked"] = unlocked
    out["unlock_window_s"] = unlock_window_s

    out["eligible"] = bool(
        batt_ok and fresh_ok_any and (not shutdown_recent) and unlocked
    )
    return out


def _phone_is_eligible(
    hass: HomeAssistant,
    notify_service: str,
    min_batt: int,
    fresh_s: int,
    unlock_window_s: int,
) -> bool:
    """Battery + freshness + not-shutdown + unlocked (sticky explicit unlock)."""
    domain, _ = _split_service(notify_service)
    if domain != "notify":
        return False

    expl = _explain_phone_eligibility(
        hass, notify_service, min_batt, fresh_s, unlock_window_s
    )
    return bool(expl["eligible"])


# ---------- PC logic (session or explicit lock sensor) ----------


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
    PC is eligible only if session sensor is fresh (or explicitly 'Unlocked')
    AND unlocked, AND (awake if required). Treat 'Unlocked' as both fresh and awake.
    """
    if not session_entity:
        return (False, False)

    st = hass.states.get(session_entity)
    if st is None:
        return (False, False)

    now_dt = dt_util.utcnow()
    ts_any = getattr(st, "last_updated", None)
    ts: Optional[datetime] = ts_any if isinstance(ts_any, datetime) else None
    age_ok = (now_dt - ts) <= timedelta(seconds=fresh_s) if ts is not None else False

    state_raw = st.state or ""
    state = state_raw.lower().strip()

    # IMPORTANT: check 'unlocked' BEFORE checking 'locked' (since 'unlocked' contains 'locked')
    if "unlocked" in state:
        is_unlocked = True
    elif "locked" in state:
        is_unlocked = False
    else:
        # Fallback heuristic: any 'unlock' hint without an explicit 'locked'
        is_unlocked = "unlock" in state

    # If explicitly unlocked, treat as fresh and awake enough
    fresh_ok = age_ok or is_unlocked
    awake = _looks_awake(state) or is_unlocked

    eligible = fresh_ok and (awake or not require_awake) and is_unlocked
    _LOGGER.debug(
        "PC session %s | state=%s fresh_ok=%s awake=%s unlocked=%s eligible=%s",
        session_entity,
        state_raw,
        fresh_ok,
        awake,
        is_unlocked,
        eligible,
    )
    return (eligible, is_unlocked)


def _pc_from_lock_entity_is_eligible(
    hass: HomeAssistant,
    lock_entity: str,
    fresh_s: int,
) -> tuple[bool, bool]:
    """
    Evaluate a PC-like candidate from a lock entity:
      e.g., binary_sensor.<slug>_screen_lock (go-hass-agent) or
            binary_sensor.<slug}_device_locked (off == unlocked) as a weak fallback.

    Unlocked if state is 'Unlocked' (case-insensitive) OR boolean off/false.
    Fresh if last_updated <= fresh_s.
    """
    st = hass.states.get(lock_entity)
    if st is None:
        return (False, False)

    val_raw = str(st.state or "")
    val = val_raw.strip().lower()

    # Interpret locked/unlocked
    if val in ("unlocked", "off", "false", "0", "no"):
        is_unlocked = True
    elif val in ("locked", "on", "true", "1", "yes"):
        is_unlocked = False
    else:
        # unknown value -> treat as locked
        is_unlocked = False

    ts_any = getattr(st, "last_updated", None)
    ts: Optional[datetime] = ts_any if isinstance(ts_any, datetime) else None
    now_dt = dt_util.utcnow()
    fresh_ok = (now_dt - ts) <= timedelta(seconds=fresh_s) if ts is not None else False

    eligible = bool(is_unlocked and fresh_ok)
    _LOGGER.debug(
        "PC (lock) %s | state=%s fresh_ok=%s unlocked=%s eligible=%s",
        lock_entity,
        val_raw,
        fresh_ok,
        is_unlocked,
        eligible,
    )
    return (eligible, is_unlocked)


def _pc_like_is_eligible(
    hass: HomeAssistant,
    service: str,
    *,
    session_entity: Optional[str],
    fresh_s: int,
    require_awake: bool,
    autodetect: bool,
) -> tuple[bool, bool, str]:
    """
    Evaluate a PC-like notify service.

    - If session_entity provided → use session-based logic.
    - Else if autodetect: try lock entity: binary_sensor.<slug>_screen_lock
    - Else try a couple of generic fallbacks (rare on PC):
        binary_sensor.{slug}_device_locked == off
        sensor.{slug}_lock_state == 'unlocked'
    Returns: (eligible, unlocked, mode) where mode in {'session','screen_lock','fallback','none'}
    """
    slug = _service_slug(service)

    # 1) session-based (Windows style)
    if session_entity:
        eligible, unlocked = _pc_is_eligible(
            hass, session_entity, fresh_s, require_awake
        )
        return (eligible, unlocked, "session")

    # 2) go-hass-agent: explicit screen_lock
    if autodetect:
        lock_entity = f"binary_sensor.{slug}_screen_lock"
        if hass.states.get(lock_entity) is not None:
            eligible, unlocked = _pc_from_lock_entity_is_eligible(
                hass, lock_entity, fresh_s
            )
            return (eligible, unlocked, "screen_lock")

    # 3) fallbacks (rare)
    #    a) device_locked (off == unlocked)
    dev_lock = f"binary_sensor.{slug}_device_locked"
    if hass.states.get(dev_lock) is not None:
        elig, unlocked = _pc_from_lock_entity_is_eligible(hass, dev_lock, fresh_s)
        return (elig, unlocked, "fallback")

    #    b) lock_state sensor
    lock_state = f"sensor.{slug}_lock_state"
    st = hass.states.get(lock_state)
    if st is not None:
        val_raw = str(st.state or "")
        val = val_raw.lower().strip()
        is_unlocked = "unlocked" in val and "locked" not in val
        ts_any = getattr(st, "last_updated", None)
        ts: Optional[datetime] = ts_any if isinstance(ts_any, datetime) else None
        now_dt = dt_util.utcnow()
        fresh_ok = (
            (now_dt - ts) <= timedelta(seconds=fresh_s) if ts is not None else False
        )
        eligible = bool(is_unlocked and fresh_ok)
        _LOGGER.debug(
            "PC (lock_state) %s | state=%s fresh_ok=%s unlocked=%s eligible=%s",
            lock_state,
            val_raw,
            fresh_ok,
            is_unlocked,
            eligible,
        )
        return (eligible, is_unlocked, "fallback")

    # None found
    return (False, False, "none")


def _is_pc_like_service(
    hass: HomeAssistant,
    service: str,
    legacy_pc_service: Optional[str],
    forced_pc_like: Set[str],
    autodetect: bool,
) -> bool:
    """Classify a service as PC-like."""
    if legacy_pc_service and service == legacy_pc_service:
        return True
    if service in forced_pc_like:
        return True
    _, svc = _split_service(service)
    slug = _service_slug(service)
    # Auto-detect: screen_lock or non-mobile_app service
    if autodetect:
        if hass.states.get(f"binary_sensor.{slug}_screen_lock") is not None:
            return True
        if not svc.startswith("mobile_app_"):
            return True
    # otherwise treat as phone
    return False


def _dedupe_preserve_order(seq: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _choose_service_smart(
    hass: HomeAssistant, cfg: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    """
    Policy semantics:

    - PC_FIRST:
        If any PC candidate eligible → pick the first eligible PC (ordered).
        Else → first eligible phone in CONF_SMART_PHONE_ORDER.
    - PHONE_FIRST:
        If any eligible phone → first eligible phone in order.
        Else → first eligible PC.
    - PHONE_IF_PC_UNLOCKED:
        If at least one PC candidate is UNLOCKED/eligible:
            prefer phones (first eligible); if none, use the first eligible PC.
        If no PC is unlocked/eligible:
            pick first eligible PC; else first eligible phone.
    """
    legacy_pc_service: str | None = cfg.get(CONF_SMART_PC_NOTIFY)
    legacy_pc_session: str | None = cfg.get(CONF_SMART_PC_SESSION)
    order: list[str] = list(cfg.get(CONF_SMART_PHONE_ORDER, []))

    min_batt = int(cfg.get(CONF_SMART_MIN_BATTERY, DEFAULT_SMART_MIN_BATTERY))
    phone_fresh = int(cfg.get(CONF_SMART_PHONE_FRESH_S, DEFAULT_SMART_PHONE_FRESH_S))
    pc_fresh = int(cfg.get(CONF_SMART_PC_FRESH_S, DEFAULT_SMART_PC_FRESH_S))
    require_pc_awake = bool(
        cfg.get(CONF_SMART_REQUIRE_AWAKE, DEFAULT_SMART_REQUIRE_AWAKE)
    )
    policy = cfg.get(CONF_SMART_POLICY, DEFAULT_SMART_POLICY)

    # Sticky unlock window (configurable via options, falls back to default)
    unlock_window_s = int(
        cfg.get(CONF_SMART_PHONE_UNLOCK_WINDOW_S, DEFAULT_SMART_PHONE_UNLOCK_WINDOW_S)
    )

    # Optional: list of services to force as PC-like
    forced_pc_like_list = cfg.get(OPT_PC_LIKE_SERVICES, []) or []
    forced_pc_like: Set[str] = {
        str(s) for s in forced_pc_like_list if isinstance(s, str)
    }

    # Optional: autodetect PC-like by screen_lock / non-mobile_app
    autodetect_pc_like = bool(cfg.get(OPT_PC_AUTODETECT, True))

    # we hard-require phone unlocked regardless of the option (kept for compat)
    _ = cfg.get(CONF_SMART_REQUIRE_PHONE_UNLOCKED, DEFAULT_SMART_REQUIRE_PHONE_UNLOCKED)

    # Split services into PC-like vs Phone-like (dynamic)
    pc_candidates_order: List[str] = []
    phone_candidates_order: List[str] = []
    for svc in order:
        if _is_pc_like_service(
            hass, svc, legacy_pc_service, forced_pc_like, autodetect_pc_like
        ):
            pc_candidates_order.append(svc)
        else:
            phone_candidates_order.append(svc)

    # Ensure legacy PC (if provided) stays first among PCs
    if legacy_pc_service:
        pc_candidates_order = _dedupe_preserve_order(
            [legacy_pc_service] + pc_candidates_order
        )

    # Evaluate PCs
    pc_evals: List[Dict[str, Any]] = []
    for svc in pc_candidates_order:
        sess = legacy_pc_session if svc == legacy_pc_service else None
        ok, unlocked, mode = _pc_like_is_eligible(
            hass,
            svc,
            session_entity=sess,
            fresh_s=pc_fresh,
            require_awake=require_pc_awake,
            autodetect=autodetect_pc_like,
        )
        pc_evals.append(
            {
                "service": svc,
                "session_entity": sess,
                "ok": ok,
                "unlocked": unlocked,
                "mode": mode,
            }
        )
    first_pc_ok: Optional[str] = next((e["service"] for e in pc_evals if e["ok"]), None)
    any_pc_unlocked_ok: bool = any(e["ok"] and e["unlocked"] for e in pc_evals)

    # Evaluate phones (sticky unlock)
    eligible_phones: list[str] = []
    eligibility_by_phone: Dict[str, Any] = {}
    for svc in phone_candidates_order:
        expl = _explain_phone_eligibility(
            hass, svc, min_batt, phone_fresh, unlock_window_s
        )
        eligibility_by_phone[svc] = expl
        if expl["eligible"]:
            eligible_phones.append(svc)

    # choose by policy
    chosen: Optional[str] = None
    if policy == SMART_POLICY_PC_FIRST:
        if first_pc_ok:
            chosen = first_pc_ok
        elif eligible_phones:
            chosen = eligible_phones[0]

    elif policy == SMART_POLICY_PHONE_FIRST:
        if eligible_phones:
            chosen = eligible_phones[0]
        elif first_pc_ok:
            chosen = first_pc_ok

    elif policy == SMART_POLICY_PHONE_IF_PC_UNLOCKED:
        if any_pc_unlocked_ok:
            chosen = eligible_phones[0] if eligible_phones else first_pc_ok
        else:
            chosen = (
                first_pc_ok
                if first_pc_ok
                else (eligible_phones[0] if eligible_phones else None)
            )

    else:
        _LOGGER.warning("Unknown smart policy %r; defaulting to PC_FIRST", policy)
        if first_pc_ok:
            chosen = first_pc_ok
        elif eligible_phones:
            chosen = eligible_phones[0]

    info = {
        "policy": policy,
        "phone_order": order,  # original declared order
        "pc_service": legacy_pc_service,
        "pc_session": legacy_pc_session,
        "pc_candidates": pc_evals,  # detailed per-PC evaluation
        "eligible_phones": eligible_phones,
        "eligibility_by_phone": eligibility_by_phone,
        "min_battery": min_batt,
        "phone_fresh_s": phone_fresh,
        "pc_fresh_s": pc_fresh,
        "require_pc_awake": require_pc_awake,
        "phones_require_unlocked": True,
        "unlock_window_s": unlock_window_s,
        "pc_like_forced": sorted(forced_pc_like),
        "pc_like_autodetect": autodetect_pc_like,
    }
    return (chosen, info)


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
            # legacy PC session (Windows)
            pc_session = cfg.get(CONF_SMART_PC_SESSION)
            if isinstance(pc_session, str) and pc_session:
                watch.add(pc_session)

            order = list(cfg.get(CONF_SMART_PHONE_ORDER, []) or [])

            # Optional PC-like config
            forced_pc_like_list = cfg.get(OPT_PC_LIKE_SERVICES, []) or []
            forced_pc_like: Set[str] = {
                str(s) for s in forced_pc_like_list if isinstance(s, str)
            }
            autodetect_pc_like = bool(cfg.get(OPT_PC_AUTODETECT, True))
            legacy_pc_service: str | None = cfg.get(CONF_SMART_PC_NOTIFY)

            for full in order:
                # Determine PC-like vs phone-like
                if _is_pc_like_service(
                    self.hass,
                    full,
                    legacy_pc_service,
                    forced_pc_like,
                    autodetect_pc_like,
                ):
                    slug = _service_slug(full)
                    # PC-like (go-hass-agent / custom)
                    scr = f"binary_sensor.{slug}_screen_lock"
                    if self.hass.states.get(scr) is not None:
                        watch.add(scr)
                    # fallbacks
                    dev = f"binary_sensor.{slug}_device_locked"
                    if self.hass.states.get(dev) is not None:
                        watch.add(dev)
                    lks = f"sensor.{slug}_lock_state"
                    if self.hass.states.get(lks) is not None:
                        watch.add(lks)
                    continue

                # Otherwise treat as a PHONE and watch phone-related sensors
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
                # phone locks / interactive / awake (raw) + hints
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
                    # hints
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


# ───────────────────────── utilities ─────────────────────────


def _config_view(entry: ConfigEntry) -> dict[str, Any]:
    cfg = dict(entry.data)
    cfg.update(entry.options or {})
    return cfg
