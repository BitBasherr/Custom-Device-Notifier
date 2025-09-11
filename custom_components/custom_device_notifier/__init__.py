from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Set, cast

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

from homeassistant.const import EVENT_SERVICE_REGISTERED

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
    # TTS
    TTS_OPT_ENABLE,
    TTS_OPT_DEFAULT,
    TTS_OPT_SERVICE,
    TTS_OPT_LANGUAGE,
    MEDIA_ORDER_OPT,
    CONF_MEDIA_PLAYER_ORDER,
    CONF_BOOT_STICKY_TARGET_S,
    _BOOT_STICKY_TARGET_S,
    CONF_MSG_ENABLE,
    CONF_MSG_SOURCE_SENSOR,
    CONF_MSG_APPS,
    CONF_MSG_TARGETS,
    CONF_MSG_REPLY_TRANSPORT,
    CONF_MSG_KDECONNECT_DEVICE_ID,
    CONF_MSG_TASKER_EVENT,
    #Messaging Specific Constants:
    DEFAULT_MSG_REPLY_TRANSPORT,
    DEFAULT_MSG_TASKER_EVENT,
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


def _boot_window_seconds(cfg: dict[str, Any]) -> int:
    try:
        return int(cfg.get(CONF_BOOT_STICKY_TARGET_S, _BOOT_STICKY_TARGET_S))
    except (TypeError, ValueError):
        return _BOOT_STICKY_TARGET_S


def _boot_window_seconds_left(cfg: dict[str, Any]) -> float:
    win = _boot_window_seconds(cfg)
    elapsed = (dt_util.utcnow() - _BOOT_UTC).total_seconds()
    return max(0.0, float(win) - float(elapsed))


async def _wait_for_service_registered(
    hass: HomeAssistant,
    domain: str,
    service: str,
    *,
    timeout_s: float,
    poll_interval_s: float = 0.2,
) -> bool:
    """
    Poll until the given service is registered or timeout elapses.
    Returns True if the service is available, else False.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    # Fast path
    if hass.services.has_service(domain, service):
        return True
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval_s)
        if hass.services.has_service(domain, service):
            return True
    # Ensure we return a real bool, not Any
    return bool(hass.services.has_service(domain, service))


async def _wait_for_service(
    hass: HomeAssistant, domain: str, service: str, timeout_s: float
) -> bool:
    """Wait up to timeout_s for (domain.service) to register."""
    if hass.services.has_service(domain, service):
        return True

    evt = asyncio.Event()

    def _cb(event) -> None:
        data = event.data or {}
        if data.get("domain") == domain and data.get("service") == service:
            evt.set()

    unsub = hass.bus.async_listen(EVENT_SERVICE_REGISTERED, _cb)
    try:
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass
        # Explicit cast to satisfy mypy
        return bool(hass.services.has_service(domain, service))
    finally:
        unsub()


def _signal_name(entry_id: str) -> str:
    """Dispatcher signal used to publish routing decisions (and previews)."""
    return f"{DOMAIN}_route_update_{entry_id}"


_STARTUP_GRACE_S = 120  # small window to ignore "restored" freshness
_BOOT_UTC: datetime = cast(datetime, dt_util.utcnow())


def _is_restored_or_boot_fresh(st: State | None) -> bool:
    """True if the state looks restored at startup (don’t trust freshness/unlock)."""
    if st is None:
        return False

    # Many RestoreEntitys add attributes["restored"]: True
    attrs = getattr(st, "attributes", None)
    if isinstance(attrs, dict) and attrs.get("restored") is True:
        return True

    # Avoid Any from getattr: type as object, then narrow
    last_updated_obj: object | None = getattr(st, "last_updated", None)
    if not isinstance(last_updated_obj, datetime):
        return False

    last_updated: datetime = last_updated_obj
    # treat anything 'updated' near boot as restored
    return bool((last_updated - _BOOT_UTC) <= timedelta(seconds=_STARTUP_GRACE_S))


@dataclass
class EntryRuntime:
    entry: ConfigEntry
    service_name: str  # notify service name (slug)
    preview: Optional["PreviewManager"] = None  # live preview publisher
    msg_bridge: Optional["MessageBridge"] = None  # messages mirror/reply

# ─────────────────────────── lifecycle ───────────────────────────


async def _maybe_play_tts(
    hass: HomeAssistant,
    entry: ConfigEntry,
    payload: dict[str, Any],
    cfg: dict[str, Any],
) -> None:
    """If TTS is enabled and requested, call the configured tts.* service."""
    if not bool(cfg.get(TTS_OPT_ENABLE)):
        return

    # ---- message to speak ----------------------------------------------------
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}

    text: Optional[str] = None

    # 1) explicit request via data.tts_text
    raw_tts = data.get("tts_text")
    if isinstance(raw_tts, (str, int, float)) and str(raw_tts).strip():
        text = str(raw_tts)
    # 2) or speak the normal message when 'Send TTS by default' is on
    elif bool(cfg.get(TTS_OPT_DEFAULT)):
        raw_msg = payload.get("message") or payload.get("title")
        if isinstance(raw_msg, (str, int, float)) and str(raw_msg).strip():
            text = str(raw_msg)

    if not text:
        return

    # ---- pick media_player ---------------------------------------------------
    mp: Optional[str] = None

    # payload override
    override = data.get("media_player_entity_id")
    if isinstance(override, str) and override:
        mp = override
    elif isinstance(override, list) and override and isinstance(override[0], str):
        mp = override[0]

    # configured order (new key first, then legacy fallback)
    if not mp:
        order_any = cfg.get(MEDIA_ORDER_OPT)
        if order_any is None:
            # legacy key fallback (kept for compatibility)
            order_any = cfg.get(CONF_MEDIA_PLAYER_ORDER)

        order: list[str] = []
        if isinstance(order_any, str):
            order = [order_any]
        elif isinstance(order_any, list):
            order = [s for s in order_any if isinstance(s, str)]

        if order:
            mp = order[0]

    if not mp:
        _LOGGER.warning(
            "TTS requested but no media player is configured/ordered "
            "(data.media_player_entity_id or %s/%s).",
            MEDIA_ORDER_OPT,
            CONF_MEDIA_PLAYER_ORDER,
        )
        return

    # ---- tts service + call --------------------------------------------------
    tts_service = cfg.get(TTS_OPT_SERVICE)
    if not isinstance(tts_service, str) or "." not in tts_service:
        _LOGGER.warning(
            "TTS requested but tts_service option is missing/invalid: %r",
            tts_service,
        )
        return

    lang = cfg.get(TTS_OPT_LANGUAGE)
    language: Optional[str] = str(lang) if isinstance(lang, str) and lang else None

    tts_domain, tts_method = tts_service.split(".", 1)

    # tts.speak uses media_player_entity_id; legacy engines (google_translate_say) use entity_id
    svc_data: dict[str, Any] = {"message": text}
    if tts_method == "speak":
        svc_data["media_player_entity_id"] = mp
    else:
        svc_data["entity_id"] = mp
        svc_data["cache"] = False
    if language:
        svc_data["language"] = language

    _LOGGER.debug("TTS: calling %s with %s", tts_service, svc_data)
    try:
        await hass.services.async_call(tts_domain, tts_method, svc_data, blocking=True)
    except Exception:
        _LOGGER.exception("TTS call %s failed", tts_service)


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data.setdefault(DATA, {})
    hass.data.setdefault(SERVICE_HANDLES, {})
    store: Store = Store(hass, _STORE_VER, _STORE_KEY)
    mem: Dict[str, Any] = await store.async_load() or {}
    hass.data[DATA]["store"] = store
    hass.data[DATA]["memory"] = mem

    # Seed sticky unlocks from disk
    for slug, ts_iso in (mem.get("last_phone_unlock", {})).items():
        try:
            parsed = dt_util.parse_datetime(ts_iso)
            if parsed is not None:
                _LAST_PHONE_UNLOCK_UTC[slug] = parsed
        except Exception:  # pragma: no cover
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
    
    # NEW: start messages bridge if enabled
    mb = MessageBridge(hass, entry)
    await mb.async_start()
    rt.msg_bridge = mb
    
    # proper unload cleanup
    async def _stop_preview() -> None:
        await pm.async_stop()
        
    async def _stop_bridge() -> None:
        if rt.msg_bridge:
            await rt.msg_bridge.async_stop()

    entry.async_on_unload(_stop_preview)
    entry.async_on_unload(_stop_bridge)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _LOGGER.info(
        "Registered notify.%s for %s", slug, entry.data.get(CONF_SERVICE_NAME_RAW, slug)
    )
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    rt: EntryRuntime | None = hass.data[DATA].get(entry.entry_id)
    if rt and rt.preview:
        await rt.preview.async_rebuild()
    if rt and rt.msg_bridge:
        await rt.msg_bridge.async_stop()
        await rt.msg_bridge.async_start()
    _LOGGER.debug("Options updated for %s", entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    rt: EntryRuntime | None = hass.data[DATA].pop(entry.entry_id, None)
    if rt:
        if rt.preview:
            await rt.preview.async_stop()
        if rt.msg_bridge:
            await rt.msg_bridge.async_stop()

    slug = hass.data[SERVICE_HANDLES].pop(entry.entry_id, None)
    if slug and hass.services.has_service("notify", slug):
        hass.services.async_remove("notify", slug)

    ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    return bool(ok)  # mypy: ensure bool


# ─────────────────────────── routing entry point ───────────────────────────


async def _route_and_forward(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]
) -> None:
    cfg = _config_view(entry)

    target_service: str = ""
    via: str = "matched"
    decision: Dict[str, Any] = {
        "timestamp": dt_util.utcnow().isoformat(),
        "mode": cfg.get(CONF_ROUTING_MODE, DEFAULT_ROUTING_MODE),
        "payload_keys": sorted(list(payload.keys())),
    }

    # Prefer the last-used target briefly after boot
    sticky = _boot_sticky_target(hass, entry)
    if sticky:
        target_service = sticky
        via = "boot-sticky"
    else:
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
            _LOGGER.warning(
                "Unknown routing mode %r, falling back to conditional", mode
            )
            svc, info = _choose_service_conditional_with_info(hass, cfg)
            decision.update({"conditional": info})
            target_service = svc or ""

    # Fallback if still nothing
    if not target_service:
        fb = cast(Optional[str], cfg.get(CONF_FALLBACK))
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
        fb = cast(Optional[str], cfg.get(CONF_FALLBACK))
        if isinstance(fb, str) and fb and fb != target_service:
            domain, service = _split_service(fb)
            via = "self-recursion-fallback"
        else:
            decision.update({"result": "dropped_self"})
            async_dispatcher_send(hass, _signal_name(entry.entry_id), decision)
            return

    # If we’re within the sticky window and selected the sticky target,
    # wait (up to the remaining sticky time) for that service to register
    if via == "boot-sticky":
        elapsed = (dt_util.utcnow() - _BOOT_UTC).total_seconds()
        remaining = max(0.0, float(_BOOT_STICKY_TARGET_S) - float(elapsed))
        if remaining > 0:
            ok = await _wait_for_service_registered(
                hass, domain, service, timeout_s=remaining
            )
            if not ok:
                # Service didn’t show up in time → compute a live choice now
                _LOGGER.debug(
                    "Sticky target %s.%s not registered within %.1fs; choosing live.",
                    domain,
                    service,
                    remaining,
                )
                mode = decision["mode"]
                chosen_now = None
                if mode == ROUTING_SMART:
                    chosen_now, info = _choose_service_smart(hass, cfg)
                    decision.update({"smart": info})
                else:
                    chosen_now, info = _choose_service_conditional_with_info(hass, cfg)
                    decision.update({"conditional": info})
                if chosen_now:
                    domain, service = _split_service(chosen_now)
                    via = "boot-sticky-failed"
                else:
                    fb = cast(Optional[str], cfg.get(CONF_FALLBACK))
                    if isinstance(fb, str) and fb:
                        domain, service = _split_service(fb)
                        via = "boot-sticky-fallback"
                    else:
                        decision.update({"result": "dropped"})
                        async_dispatcher_send(
                            hass, _signal_name(entry.entry_id), decision
                        )
                        _LOGGER.warning(
                            "Sticky failed, no live match, no fallback; dropping."
                        )
                        return

    decision.update(
        {
            "result": "forwarded",
            "service_full": f"{domain}.{service}",
            "via": via,
        }
    )
    async_dispatcher_send(hass, _signal_name(entry.entry_id), decision)

    # Persist last-used target so early post-boot notifications can stick to it
    _save_last_target(hass, f"{domain}.{service}")

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
    latest_lock_ts, latest_fresh_unlock_ts, latest_any_unlock_ts = (
        _explicit_unlock_times(hass, slug, fresh_s)
    )
    now_dt = dt_util.utcnow()

    # helpers for persistence
    mem: Dict[str, Any] = cast(
        Dict[str, Any], hass.data.get(DATA, {}).setdefault("memory", {})
    )
    store: Optional[Store] = cast(Optional[Store], hass.data.get(DATA, {}).get("store"))

    def _persist(ts: datetime) -> None:
        d = cast(Dict[str, str], mem.setdefault("last_phone_unlock", {}))
        d[slug] = ts.isoformat()
        if store:
            hass.async_create_task(store.async_save(mem))

    # Fresh explicit unlock wins
    if latest_fresh_unlock_ts is not None:
        _LAST_PHONE_UNLOCK_UTC[slug] = latest_fresh_unlock_ts
        _persist(latest_fresh_unlock_ts)
        return True

    # Stale explicit unlock can seed if phone itself is fresh and no newer lock exists
    if fresh_ok_any and latest_any_unlock_ts is not None:
        if latest_lock_ts is None or latest_any_unlock_ts > latest_lock_ts:
            _LAST_PHONE_UNLOCK_UTC[slug] = latest_any_unlock_ts
            _persist(latest_any_unlock_ts)
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
    legacy_pc_service: str | None = cast(Optional[str], cfg.get(CONF_SMART_PC_NOTIFY))
    legacy_pc_session: str | None = cast(Optional[str], cfg.get(CONF_SMART_PC_SESSION))
    order: List[str] = list(cast(List[str], cfg.get(CONF_SMART_PHONE_ORDER, [])))

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
    eligible_phones: List[str] = []
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


class MessageBridge:
    """Mirror SMS/IM notifications from one phone and allow inline reply."""

    ACTION = "CDN_REPLY_MESSAGE"  # action id we emit on actionable notifs

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._unsub_sensor: Optional[Callable[[], None]] = None
        self._unsub_action: Optional[Callable[[], None]] = None

    def _cfg(self) -> dict[str, Any]:
        return _config_view(self.entry)

    async def async_start(self) -> None:
        cfg = self._cfg()
        if not bool(cfg.get(CONF_MSG_ENABLE)):
            return
        sensor_id = str(cfg.get(CONF_MSG_SOURCE_SENSOR) or "")
        if not sensor_id:
            _LOGGER.debug("MessagesBridge: source sensor not configured")
            return

        # Listen for new notifications on the source device
        self._unsub_sensor = async_track_state_change_event(
            self.hass, [sensor_id], self._on_last_notification
        )

        # Listen for reply actions coming back from any device
        self._unsub_action = self.hass.bus.async_listen(
            "mobile_app_notification_action", self._on_mobile_action
        )
        _LOGGER.info("MessagesBridge started for %s", sensor_id)

    async def async_stop(self) -> None:
        if self._unsub_sensor:
            self._unsub_sensor()
            self._unsub_sensor = None
        if self._unsub_action:
            self._unsub_action()
            self._unsub_action = None
        _LOGGER.info("MessagesBridge stopped")

    # ---------- helpers ----------

    def _apps_set(self) -> set[str]:
        cfg = self._cfg()
        apps = cfg.get(CONF_MSG_APPS) or []
        return {str(p) for p in apps if isinstance(p, str) and p}

    def _targets(self) -> list[str]:
        cfg = self._cfg()
        raw = cfg.get(CONF_MSG_TARGETS) or []
        return [str(s) for s in raw if isinstance(s, str) and s]

    # ---------- handlers ----------

    @callback
    def _on_last_notification(self, event) -> None:
        """Mirror qualifying notifications as actionable messages."""
        try:
            new_state: Optional[State] = event.data.get("new_state")
            if not new_state:
                return

            attrs = dict(getattr(new_state, "attributes", {}) or {})
            a = dict(attrs.get("android") or {})
            pkg = str(a.get("package") or "")
            if not pkg:
                return

            apps = self._apps_set()
            if apps and pkg not in apps:
                return

            # Extract message fields
            title = (
                a.get("conversation_title")
                or a.get("title")
                or attrs.get("title")
                or "New message"
            )
            text = a.get("text") or attrs.get("text") or ""
            if not str(text).strip():
                return  # nothing to show

            # Conversation id / number (best effort; adjust if your app exposes different fields)
            conv_id = (
                a.get("conversation_id")
                or a.get("tag")
                or a.get("post_time")
                or new_state.last_changed.isoformat()
            )
            number = a.get("phone") or a.get("person") or ""

            # Build a replyable notification and send to configured targets
            payload = {
                "title": str(title),
                "message": str(text),
                "data": {
                    "tag": f"msg_{conv_id}",
                    "channel": "messages",
                    "actions": [
                        {
                            "action": self.ACTION,
                            "title": "Reply",
                            "reply": True,
                            "placeholder": "Send from Fold 7…",
                            "action_data": {
                                "number": number,
                                "conv_id": conv_id,
                                "package": pkg,
                            },
                        }
                    ],
                },
            }

            targets = self._targets()
            if not targets:
                # Default to our own notify service to benefit from your routing
                own_slug = str(self.entry.data.get(CONF_SERVICE_NAME) or "")
                if own_slug:
                    self.hass.async_create_task(
                        self.hass.services.async_call(
                            "notify", own_slug, payload, blocking=False
                        )
                    )
                return

            for full in targets:
                domain, service = _split_service(full)
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        domain, service, payload, blocking=False
                    )
                )
        except Exception:  # pragma: no cover
            _LOGGER.exception("MessagesBridge: error mirroring notification")

    async def _send_reply(self, number: str, text: str, pkg: str, conv_id: str) -> None:
        """Send the reply using the configured transport."""
        cfg = self._cfg()
        transport = str(cfg.get(CONF_MSG_REPLY_TRANSPORT) or DEFAULT_MSG_REPLY_TRANSPORT)

        if transport == "kdeconnect":
            device_id = str(cfg.get(CONF_MSG_KDECONNECT_DEVICE_ID) or "")
            if not device_id:
                _LOGGER.warning("MessagesBridge: KDE Connect device_id not set")
                return
            await self.hass.services.async_call(
                "kdeconnect",
                "send_sms",
                {"device_id": device_id, "number": number, "message": text},
                blocking=False,
            )
            return

        # tasker/autonotification route: fire an HA event Tasker can listen for
        event_name = str(cfg.get(CONF_MSG_TASKER_EVENT) or DEFAULT_MSG_TASKER_EVENT)
        self.hass.bus.async_fire(
            event_name,
            {
                "package": pkg,
                "conversation_id": conv_id,
                "number": number,
                "text": text,
            },
        )

    @callback
    def _on_mobile_action(self, event) -> None:
        """Handle inline replies coming back from HA actionable notifications."""
        try:
            data = dict(event.data or {})
            if data.get("action") != self.ACTION:
                return
            action_data = dict(data.get("action_data") or {})
            reply = data.get("reply_text") or data.get("text") or ""
            number = action_data.get("number") or ""
            pkg = action_data.get("package") or ""
            conv_id = action_data.get("conv_id") or ""
            if not str(reply).strip():
                return
            # Schedule the actual send
            self.hass.async_create_task(
                self._send_reply(str(number), str(reply), str(pkg), str(conv_id))
            )
        except Exception:  # pragma: no cover
            _LOGGER.exception("MessagesBridge: error handling reply")


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
        # Honor boot-sticky in the preview as well so the sensor reflects reality
        sticky = _boot_sticky_target(self.hass, self.entry)
        if sticky:
            chosen = sticky
            decision["via"] = "preview-boot-sticky"
        else:
            if mode == ROUTING_SMART:
                chosen, info = _choose_service_smart(self.hass, cfg)
                decision["smart"] = info
            else:
                chosen, info = _choose_service_conditional_with_info(self.hass, cfg)
                decision["conditional"] = info

        if not chosen:
            # show fallback if present (this matches UI expectations for "what would happen now")
            fb = cast(Optional[str], cfg.get(CONF_FALLBACK))
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


def _save_last_target(hass: HomeAssistant, service_full: str) -> None:
    """Persist the last chosen notify target so we can reuse it right after boot."""
    mem = hass.data.get(DATA, {}).setdefault("memory", {})
    mem["last_target_service"] = service_full

    store_obj = hass.data.get(DATA, {}).get("store")
    if isinstance(store_obj, Store):
        hass.async_create_task(store_obj.async_save(mem))


def _boot_sticky_target(hass: HomeAssistant, entry: ConfigEntry) -> Optional[str]:
    """
    During the first _BOOT_STICKY_TARGET_S seconds after boot, prefer the last
    target we actually forwarded to (if present and not ourselves).
    NOTE: We do NOT require the service to be registered here; we’ll wait
    for it later in _route_and_forward when sending.
    """
    if (dt_util.utcnow() - _BOOT_UTC) > timedelta(seconds=_BOOT_STICKY_TARGET_S):
        return None

    mem = hass.data.get(DATA, {}).get("memory", {})
    sticky = mem.get("last_target_service")
    if not isinstance(sticky, str) or "." not in sticky:
        return None

    domain, service = _split_service(sticky)
    own_slug = str(entry.data.get(CONF_SERVICE_NAME) or "")
    if domain == "notify" and service == own_slug:
        return None  # never recurse into ourselves

    return sticky
