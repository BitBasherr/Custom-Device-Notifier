from __future__ import annotations
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Optional

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

# Policies
SMART_POLICY_PC_FIRST = "pc_first"
SMART_POLICY_PHONE_IF_PC_UNLOCKED = "phone_if_pc_unlocked"
SMART_POLICY_PHONE_FIRST = "phone_first"

SCREEN_OFF = "android.intent.action.SCREEN_OFF"
SHUTDOWN = "android.intent.action.ACTION_SHUTDOWN"

@dataclass(frozen=True)
class DeviceSpec:
    notify_target: str                # "notify.mobile_app_*"
    last_trigger: Optional[str]       # "sensor.*_last_update_trigger"
    locked_binary: Optional[str]      # "binary_sensor.*_device_locked"
    battery_sensor: Optional[str]     # "sensor.*_battery_level"

def _is_fresh(state: Optional[State], window_s: int) -> bool:
    if state is None or state.last_changed is None:
        return False
    return (dt_util.utcnow() - state.last_changed) <= timedelta(seconds=window_s)

def _safe(state: Optional[State]) -> str:
    if state is None:
        return ""
    s = state.state
    return "" if s in (None, "", "unknown", "unavailable") else str(s)

def _pc_usable(hass: HomeAssistant, session_sensor: Optional[str], fresh_s: int) -> bool:
    if not session_sensor:
        return False
    st = hass.states.get(session_sensor)
    return _safe(st) == "Unlocked" and _is_fresh(st, fresh_s)

def _phone_usable(hass: HomeAssistant, spec: DeviceSpec, *, min_batt: int, fresh_s: int,
                  require_awake: bool, require_unlocked: bool) -> bool:
    if not (spec.last_trigger and spec.locked_binary and spec.battery_sensor):
        return False
    trig = hass.states.get(spec.last_trigger)
    locked = hass.states.get(spec.locked_binary)
    batt = hass.states.get(spec.battery_sensor)
    if not (trig and locked and batt) or not _is_fresh(trig, fresh_s):
        return False
    if require_awake and _safe(trig) in ("", SHUTDOWN, SCREEN_OFF):
        return False
    if require_unlocked and _safe(locked) != "off":  # your lock sensors: off == unlocked
        return False
    try:
        if int(float(_safe(batt) or "0")) < int(min_batt):
            return False
    except ValueError:
        return False
    return True

def _spec_from_service(svc: str) -> Optional[DeviceSpec]:
    if not svc.startswith("notify.mobile_app_"):
        return None
    base = svc.split(".", 1)[1].removeprefix("mobile_app_")
    return DeviceSpec(
        notify_target=svc,
        last_trigger=f"sensor.{base}_last_update_trigger",
        locked_binary=f"binary_sensor.{base}_device_locked",
        battery_sensor=f"sensor.{base}_battery_level",
    )

def choose_best_target(
    hass: HomeAssistant,
    *,
    pc_notify_target: Optional[str],
    pc_session_sensor: Optional[str],
    phones_in_priority: Iterable[str],  # iterable of "notify.mobile_app_*"
    min_battery: int,
    phone_fresh_s: int,
    pc_fresh_s: int,
    require_awake: bool,
    require_unlocked: bool,
    policy: str,
) -> Optional[str]:
    specs = [s for s in (_spec_from_service(p) for p in phones_in_priority) if s]
    pc_ok = _pc_usable(hass, pc_session_sensor, pc_fresh_s)

    def first_phone() -> Optional[str]:
        for sp in specs:
            if _phone_usable(hass, sp, min_batt=min_battery, fresh_s=phone_fresh_s,
                             require_awake=require_awake, require_unlocked=require_unlocked):
                return sp.notify_target
        return None

    if policy == SMART_POLICY_PC_FIRST:
        return pc_notify_target if (pc_ok and pc_notify_target) else first_phone()

    if policy == SMART_POLICY_PHONE_IF_PC_UNLOCKED:
        if pc_ok:
            return first_phone() or pc_notify_target
        return first_phone()

    if policy == SMART_POLICY_PHONE_FIRST:
        return first_phone() or (pc_notify_target if pc_ok else None)

    return None
